"""Headless FPS benchmark for the Endoscopy POC architecture.

Keeps the POC's winning technique — the Basler acquisition thread on its own
dedicated (optionally isolated) CPU core, OpenVINO inference on separate cores
— but drops the 119 Hz vsync + OpenCV display coupling so it runs over plain
SSH with no monitor. Uses the GPU-compiled OpenVINO model directly (not the
Ultralytics wrapper), so `--device GPU` really runs on the GPU.

    sudo .venv/bin/python bench_headless.py --device GPU --resolution 1280,720

Reports camera acquisition FPS and inference FPS separately every second; if
the camera FPS stays high while inference runs, the dedicated-core approach
has removed the in-process pylon<->OpenVINO contention.
"""
from __future__ import annotations

import argparse
import os
import queue
import threading
import time

import cv2
import numpy as np
import openvino as ov
from pypylon import pylon


def set_rt(cpu: int, prio: int = 80) -> None:
    """Pin the calling thread to `cpu`; try SCHED_FIFO (needs root)."""
    try:
        os.sched_setaffinity(0, {cpu})
    except Exception as exc:  # noqa: BLE001
        print(f"[rt] affinity cpu{cpu} failed: {exc}")
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(prio))
        print(f"[rt] thread pinned to cpu{cpu} SCHED_FIFO prio {prio}")
    except Exception as exc:  # noqa: BLE001
        print(f"[rt] SCHED_FIFO on cpu{cpu} not applied ({exc}); affinity only")


_frame_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=2)
_stop = threading.Event()
_counts_lock = threading.Lock()
_cam_count = 0
_inf_count = 0


def camera_loop(width: int, height: int, cpu: int, exposure_us: float) -> None:
    global _cam_count
    set_rt(cpu, 85)
    cam = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
    cam.Open()
    print(f"[cam] {cam.GetDeviceInfo().GetModelName()} "
          f"sn={cam.GetDeviceInfo().GetSerialNumber()}")

    def _try(fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

    _try(lambda: cam.Width.SetValue(min(width, cam.Width.GetMax())))
    _try(lambda: cam.Height.SetValue(min(height, cam.Height.GetMax())))
    _try(lambda: cam.ExposureAuto.SetValue("Off"))
    _try(lambda: cam.GainAuto.SetValue("Off"))
    _try(lambda: cam.ExposureTime.SetValue(float(exposure_us)))

    conv = pylon.ImageFormatConverter()
    conv.OutputPixelFormat = pylon.PixelType_BGR8packed
    conv.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

    cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
    print(f"[cam] grabbing {cam.Width.GetValue()}x{cam.Height.GetValue()} "
          f"(free-run, LatestImageOnly)")
    while cam.IsGrabbing() and not _stop.is_set():
        grab = cam.RetrieveResult(2000, pylon.TimeoutHandling_Return)
        if grab and grab.GrabSucceeded():
            img = conv.Convert(grab).GetArray()
            with _counts_lock:
                _cam_count += 1
            try:
                _frame_q.put_nowait(img)
            except queue.Full:
                pass
        if grab:
            grab.Release()
    cam.StopGrabbing()
    cam.Close()


def infer_loop(model_xml: str, device: str, cpu: int) -> None:
    global _inf_count
    set_rt(cpu, 80)
    core = ov.Core()
    model = core.read_model(model_xml)
    cfg = {"NUM_STREAMS": "1", "PERFORMANCE_HINT": "LATENCY"}
    if device == "GPU":
        cfg["GPU_DISABLE_WINOGRAD_CONVOLUTION"] = "YES"
    compiled = core.compile_model(model, device, cfg)
    inp = compiled.inputs[0]
    shape = list(inp.shape)  # NCHW
    _, _, in_h, in_w = shape
    req = compiled.create_infer_request()
    print(f"[inf] compiled on {device}; input {shape}")

    while not _stop.is_set():
        try:
            img = _frame_q.get(timeout=0.5)
        except queue.Empty:
            continue
        r = cv2.resize(img, (in_w, in_h))
        r = cv2.cvtColor(r, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        r = np.ascontiguousarray(r.transpose(2, 0, 1)[None])
        req.infer({inp: r})
        with _counts_lock:
            _inf_count += 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", choices=["CPU", "GPU", "NPU"], default="GPU")
    ap.add_argument("--resolution", default="1280,720", help="width,height")
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--cam-cpu", type=int, default=4)
    ap.add_argument("--inf-cpu", type=int, default=8)
    ap.add_argument("--exposure-us", type=float, default=3000.0)
    ap.add_argument("--model", default="best_openvino_model/best.xml")
    args = ap.parse_args()
    width, height = (int(x) for x in args.resolution.split(","))

    print("=" * 60)
    print(f"Headless bench — device={args.device} res={width}x{height} "
          f"cam_cpu={args.cam_cpu} inf_cpu={args.inf_cpu}")
    print("=" * 60)

    threading.Thread(target=camera_loop,
                     args=(width, height, args.cam_cpu, args.exposure_us),
                     daemon=True).start()
    threading.Thread(target=infer_loop,
                     args=(args.model, args.device, args.inf_cpu),
                     daemon=True).start()

    t0 = time.time()
    last = t0
    last_cam = last_inf = 0
    while time.time() - t0 < args.duration:
        time.sleep(1.0)
        now = time.time()
        with _counts_lock:
            cam, inf = _cam_count, _inf_count
        dt = now - last
        print(f"[{now - t0:5.1f}s] camera={(cam - last_cam) / dt:6.1f} fps   "
              f"inference={(inf - last_inf) / dt:6.1f} fps")
        last_cam, last_inf, last = cam, inf, now

    _stop.set()
    time.sleep(0.5)
    total = time.time() - t0
    print("-" * 60)
    print(f"avg camera={_cam_count / total:.1f} fps   "
          f"avg inference={_inf_count / total:.1f} fps   over {total:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
