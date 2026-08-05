"""Direct pypylon + OpenVINO inference engine (DL Streamer-free Basler path).

This is the application form of the endoscopy POC / headless bench: the Basler
camera runs on its own dedicated (optionally isolated) CPU core, OpenVINO GPU
inference runs on separate cores, and results are drawn onto the frame — all in
one process, with no GStreamer / DL Streamer and no 119 Hz vsync coupling.

`BaslerEngine` exposes `start()` / `stop()` and feeds per-frame latency into the
same `RollingLatency` the launcher already serves at `GET /latency`, so the
backend/UI contract is unchanged. It also prints `FpsCounter(...)` lines so the
existing `docker logs | grep FpsCounter` tooling keeps working.

Standalone (benchmark / smoke test):

    sudo .venv/bin/python basler_engine.py --device GPU --resolution 1280,720
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import sys
import threading
import time

import cv2
import numpy as np
import openvino as ov
from pypylon import pylon

log = logging.getLogger("basler_engine")


def _set_rt(cpu: int | None, prio: int) -> None:
    if cpu is None:
        return
    try:
        os.sched_setaffinity(0, {cpu})
    except Exception as exc:  # noqa: BLE001
        log.warning("[rt] affinity cpu%s failed: %s", cpu, exc)
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(prio))
    except Exception as exc:  # noqa: BLE001
        log.info("[rt] SCHED_FIFO cpu%s not applied (%s); affinity only", cpu, exc)


def _letterbox_decode(output: np.ndarray, frame_w: int, frame_h: int,
                      in_w: int, in_h: int, conf_thr: float):
    """Decode a raw YOLO11 output [1, 4+nc, N] into (boxes, scores).

    Returns pixel-space xyxy boxes in the ORIGINAL frame and their scores.
    Assumes a plain resize (no letterbox padding) was used for preprocessing.
    """
    pred = output[0]                       # [4+nc, N]
    if pred.shape[0] < pred.shape[1]:      # ensure [N, 4+nc]
        pred = pred.transpose(1, 0)
    boxes_cxcywh = pred[:, :4]
    scores_all = pred[:, 4:]
    class_ids = scores_all.argmax(axis=1)
    confs = scores_all.max(axis=1)
    keep = confs >= conf_thr
    if not np.any(keep):
        return [], [], []
    boxes_cxcywh = boxes_cxcywh[keep]
    confs = confs[keep]
    class_ids = class_ids[keep]
    sx = frame_w / in_w
    sy = frame_h / in_h
    xyxy = np.empty_like(boxes_cxcywh)
    xyxy[:, 0] = (boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2) * sx
    xyxy[:, 1] = (boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2) * sy
    xyxy[:, 2] = (boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2) * sx
    xyxy[:, 3] = (boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2) * sy
    rects = [[int(x1), int(y1), int(x2 - x1), int(y2 - y1)] for x1, y1, x2, y2 in xyxy]
    idxs = cv2.dnn.NMSBoxes(rects, confs.tolist(), conf_thr, 0.45)
    if len(idxs) == 0:
        return [], [], []
    idxs = np.array(idxs).flatten()
    return xyxy[idxs].astype(int), confs[idxs], class_ids[idxs]


class BaslerEngine:
    def __init__(
        self,
        *,
        model_xml: str,
        device: str = "GPU",
        width: int = 1280,
        height: int = 720,
        threshold: float = 0.5,
        display_view: bool = False,
        video_sink: str = "xvimagesink",
        cam_cpu: int | None = None,
        inf_cpu: int | None = None,
        exposure_us: float | None = None,
        latency=None,
        emit_tracer: bool = False,
        fps_interval_s: float = 1.0,
    ) -> None:
        self.model_xml = model_xml
        self.device = device.upper()
        self.width = width
        self.height = height
        self.threshold = threshold
        self.display_view = display_view
        self.video_sink = video_sink
        self.cam_cpu = cam_cpu
        self.inf_cpu = inf_cpu
        self.exposure_us = exposure_us
        self._latency = latency
        self._emit_tracer = emit_tracer
        self._fps_interval_s = fps_interval_s

        self._stop = threading.Event()
        self._frame_q: "queue.Queue" = queue.Queue(maxsize=2)
        self._threads: list[threading.Thread] = []
        self._det_lock = threading.Lock()
        self._latest_det = None       # (boxes, scores, ids, ts_ns)
        self._count_lock = threading.Lock()
        self._inf_frames = 0          # frames actually inferred (throughput)

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._camera_loop, daemon=True, name="cam"),
            threading.Thread(target=self._infer_loop, daemon=True, name="infer"),
            threading.Thread(target=self._output_loop, daemon=True, name="output"),
            threading.Thread(target=self._fps_loop, daemon=True, name="fps"),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads = []

    def is_running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # ---- threads ----------------------------------------------------------
    def _camera_loop(self) -> None:
        _set_rt(self.cam_cpu, 85)
        cam = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
        cam.Open()
        log.info("[cam] %s sn=%s", cam.GetDeviceInfo().GetModelName(),
                 cam.GetDeviceInfo().GetSerialNumber())

        def _try(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

        _try(lambda: cam.Width.SetValue(min(self.width, cam.Width.GetMax())))
        _try(lambda: cam.Height.SetValue(min(self.height, cam.Height.GetMax())))
        if self.exposure_us is not None:
            _try(lambda: cam.ExposureAuto.SetValue("Off"))
            _try(lambda: cam.ExposureTime.SetValue(float(self.exposure_us)))

        conv = pylon.ImageFormatConverter()
        conv.OutputPixelFormat = pylon.PixelType_BGR8packed
        conv.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

        cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        log.info("[cam] grabbing %dx%d free-run", cam.Width.GetValue(), cam.Height.GetValue())
        while cam.IsGrabbing() and not self._stop.is_set():
            grab = cam.RetrieveResult(2000, pylon.TimeoutHandling_Return)
            if grab and grab.GrabSucceeded():
                img = conv.Convert(grab).GetArray()
                packet = {"image": img, "t_cap_ns": time.perf_counter_ns()}
                try:
                    self._frame_q.put_nowait(packet)
                except queue.Full:
                    pass
            if grab:
                grab.Release()
        cam.StopGrabbing()
        cam.Close()
        log.info("[cam] stopped")

    def _infer_loop(self) -> None:
        _set_rt(self.inf_cpu, 80)
        core = ov.Core()
        model = core.read_model(self.model_xml)
        cfg = {"NUM_STREAMS": "1", "PERFORMANCE_HINT": "LATENCY"}
        if self.device == "GPU":
            cfg["GPU_DISABLE_WINOGRAD_CONVOLUTION"] = "YES"
        compiled = core.compile_model(model, self.device, cfg)
        inp = compiled.inputs[0]
        _, _, in_h, in_w = list(inp.shape)
        req = compiled.create_infer_request()
        out_port = compiled.outputs[0]
        log.info("[inf] compiled on %s input=%s", self.device, list(inp.shape))

        while not self._stop.is_set():
            try:
                packet = self._frame_q.get(timeout=0.5)
            except queue.Empty:
                continue
            img = packet["image"]
            fh, fw = img.shape[:2]
            blob = cv2.resize(img, (in_w, in_h))
            blob = cv2.cvtColor(blob, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            blob = np.ascontiguousarray(blob.transpose(2, 0, 1)[None])
            req.infer({inp: blob})
            out = req.get_tensor(out_port).data
            boxes, scores, ids = _letterbox_decode(out, fw, fh, in_w, in_h, self.threshold)
            with self._det_lock:
                self._latest_det = (boxes, scores, ids, packet["t_cap_ns"])
            with self._count_lock:
                self._inf_frames += 1
            lat_ns = time.perf_counter_ns() - packet["t_cap_ns"]
            if self._latency is not None:
                self._latency.add(lat_ns / 1e6)
            if self._emit_tracer:
                # gst-tracer-format line so launcher.pump_stream folds it into /latency.
                sys.stderr.write(f"latency, time=(guint64){lat_ns}\n")
                sys.stderr.flush()

    def _output_loop(self) -> None:
        win = None
        if self.display_view:
            try:
                cv2.namedWindow("Surgical", cv2.WINDOW_NORMAL)
                win = "Surgical"
            except cv2.error as exc:
                log.warning("[out] display unavailable (%s); running headless", exc)
                win = None
        while not self._stop.is_set():
            det = None
            with self._det_lock:
                if self._latest_det is not None:
                    det = self._latest_det
            if det is None:
                time.sleep(0.005)
                continue
            boxes, scores, _ids, _ts = det
            if win is not None:
                # Re-grab latest frame is skipped; overlay on a blank of frame size
                # is not useful — draw on the freshest queued frame if present.
                frame = None
                try:
                    frame = self._frame_q.queue[-1]["image"].copy()  # peek newest
                except Exception:  # noqa: BLE001
                    frame = None
                if frame is not None:
                    for (x1, y1, x2, y2), sc in zip(boxes, scores):
                        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                        cv2.putText(frame, f"{sc:.2f}", (x1, max(0, y1 - 6)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                    cv2.imshow(win, frame)
                    if cv2.waitKey(1) == 27:
                        self._stop.set()
            time.sleep(0.001)
        if win is not None:
            cv2.destroyAllWindows()

    def _fps_loop(self) -> None:
        last = time.time()
        last_n = 0
        while not self._stop.is_set():
            time.sleep(self._fps_interval_s)
            now = time.time()
            with self._count_lock:
                n = self._inf_frames
            fps = (n - last_n) / (now - last)
            # Match the DL Streamer log shape so existing tooling keeps working.
            print(f"FpsCounter(last {now - last:.2f}sec): total={fps:.2f} fps, "
                  f"number-streams=1, per-stream={fps:.2f} fps", flush=True)
            last_n, last = n, now


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", choices=["CPU", "GPU", "NPU"], default="GPU")
    ap.add_argument("--resolution", default="1280,720")
    ap.add_argument("--model", default="best_openvino_model/best.xml")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--cam-cpu", type=int, default=None)
    ap.add_argument("--inf-cpu", type=int, default=None)
    ap.add_argument("--display", action="store_true")
    ap.add_argument("--emit-tracer", action="store_true",
                    help="emit gst-format latency lines on stderr (launcher mode)")
    args = ap.parse_args()
    w, h = (int(x) for x in args.resolution.split(","))

    from latency_tracer_sink import RollingLatency  # local import for standalone
    lat = RollingLatency()
    eng = BaslerEngine(model_xml=args.model, device=args.device, width=w, height=h,
                       threshold=args.threshold, display_view=args.display,
                       cam_cpu=args.cam_cpu, inf_cpu=args.inf_cpu, latency=lat,
                       emit_tracer=args.emit_tracer)
    stop_flag = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stop_flag.__setitem__("v", True))
    signal.signal(signal.SIGINT, lambda *_: stop_flag.__setitem__("v", True))
    eng.start()
    t0 = time.time()
    try:
        while eng.is_running() and not stop_flag["v"]:
            if args.duration > 0 and time.time() - t0 >= args.duration:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    eng.stop()
    print("latency:", lat.snapshot())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
