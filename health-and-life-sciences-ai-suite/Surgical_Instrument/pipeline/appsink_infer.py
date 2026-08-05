from __future__ import annotations

import logging
import os
import signal
import sys
import time

import gi  # type: ignore[reportMissingImports]
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # type: ignore[reportMissingImports]  # noqa: E402

from overlay_state import LatestDetections
from ov_detector import FrameMeta, OpenVINODetector
from pipeline_string import _build_source

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
log = logging.getLogger("appsink_infer")

VIDEO = os.environ.get("VIDEO", "/videos/polyp_test.mp4")
IR_XML = os.environ.get("IR_XML", "/models/yolo11n_polyp/best_openvino_model/best.xml")
SOURCE_KIND = os.environ.get("SOURCE_KIND", "file").lower()
SOURCE_ARG = os.environ.get("SOURCE_ARG", VIDEO)
DEVICE = os.environ.get("DETECTION_DEVICE", os.environ.get("DEVICE", "GPU")).upper()
TARGET_FPS = int(os.environ.get("TARGET_FPS", "60"))
THRESHOLD = float(os.environ.get("INFER_THRESHOLD", os.environ.get("THRESHOLD", "0.5")))
IOU_THRESHOLD = float(os.environ.get("INFER_IOU", "0.45"))
OV_NIREQ = max(1, int(os.environ.get("OV_NIREQ", "4")))
DISPLAY_VIEW = os.environ.get("PIPELINE_DISPLAY_VIEW", "0").strip().lower() in {"1", "true", "yes", "on"}
VIDEO_SINK = os.environ.get("PIPELINE_VIDEO_SINK", "autovideosink")
PIPELINE_SINK_SYNC = os.environ.get("PIPELINE_SINK_SYNC", "").strip().lower()
# PIPELINE_DISPLAY_* are the public knob names; APPSINK_DISPLAY_* kept as fallback.
APPSINK_DISPLAY_WIDTH = int(os.environ.get("PIPELINE_DISPLAY_WIDTH", os.environ.get("APPSINK_DISPLAY_WIDTH", "1280")))
APPSINK_DISPLAY_HEIGHT = int(os.environ.get("PIPELINE_DISPLAY_HEIGHT", os.environ.get("APPSINK_DISPLAY_HEIGHT", "720")))
APPSINK_DISPLAY_FORMAT = (os.environ.get("PIPELINE_DISPLAY_FORMAT", os.environ.get("APPSINK_DISPLAY_FORMAT", "BGRx")).strip() or "BGRx")
# Inference-branch caps. Must be an aspect-correct downscale of the 16:9 source
# (1280x720 -> 640x360) so the detector can letterbox-pad it to the square
# model input (640x640) without distortion. Do NOT set this to 640x640.
INFER_INPUT_WIDTH = int(os.environ.get("INFER_INPUT_WIDTH", "640"))
INFER_INPUT_HEIGHT = int(os.environ.get("INFER_INPUT_HEIGHT", "360"))
BASLER_PIXEL_FORMAT = os.environ.get("BASLER_PIXEL_FORMAT", "ycbcr422_8").strip() or "ycbcr422_8"
BASLER_FIXED_CAMERA = os.environ.get("BASLER_FIXED_CAMERA", "0").strip().lower() not in {"0", "false", "no"}
BASLER_EXPOSURE_US = os.environ.get("BASLER_EXPOSURE_US", "").strip()
BASLER_GAIN = os.environ.get("BASLER_GAIN", "").strip()

latest = LatestDetections()
loop: GLib.MainLoop | None = None
detector: OpenVINODetector | None = None
display_size = {"w": 1280, "h": 720}
counts = {"samples": 0, "submitted": 0, "draws": 0}
last_report = {"t": time.monotonic(), "samples": 0, "submitted": 0, "draws": 0}


def _sink_sync(source_kind: str) -> str:
    if PIPELINE_SINK_SYNC:
        return "true" if PIPELINE_SINK_SYNC in {"1", "true", "yes", "on"} else "false"
    return "false" if source_kind == "basler" else "true"


def _parse_launch_element(element: str) -> str:
    # pipeline_string.py quotes caps containing parentheses for gst-launch shell
    # safety. Gst.parse_launch receives the string directly, so those shell-only
    # quotes become syntax errors and must be removed here.
    if len(element) >= 2 and element[0] == element[-1] == "'":
        return element[1:-1]
    return element


def _fixate_basler_va_caps(element: str) -> str:
    if SOURCE_KIND == "basler" and element == "video/x-raw(memory:VAMemory),format=NV12":
        return "video/x-raw(memory:VAMemory),format=NV12,width=1280,height=720"
    return element


def _caps_wh(caps: Gst.Caps) -> tuple[int, int]:
    st = caps.get_structure(0)
    ok_w, width = st.get_int("width")
    ok_h, height = st.get_int("height")
    if not ok_w or not ok_h:
        raise ValueError(f"caps missing width/height: {caps.to_string()}")
    return int(width), int(height)


def _build_pipeline() -> str:
    source, _ = _build_source(
        SOURCE_KIND,
        SOURCE_ARG,
        TARGET_FPS,
        pre_proc_backend="va-surface-sharing",
        basler_pixel_format=BASLER_PIXEL_FORMAT,
        basler_fixed_camera=BASLER_FIXED_CAMERA,
        basler_exposure_us=BASLER_EXPOSURE_US or None,
        basler_gain=BASLER_GAIN or None,
    )
    source_chain = [_fixate_basler_va_caps(_parse_launch_element(element)) for element in source]
    if SOURCE_KIND == "file":
        source_chain.extend(["vapostproc", "video/x-raw(memory:VAMemory),format=NV12"])

    sink = f"{VIDEO_SINK} sync={_sink_sync(SOURCE_KIND)}" if DISPLAY_VIEW else "fakesink sync=false async=false"
    display_branch = " ! ".join([
        "t.",
        "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream",
        "vapostproc",
        f"video/x-raw,format={APPSINK_DISPLAY_FORMAT},width={APPSINK_DISPLAY_WIDTH},height={APPSINK_DISPLAY_HEIGHT}",
        "cairooverlay name=overlay",
        "gvafpscounter interval=1",
        sink,
    ])
    infer_branch = " ! ".join([
        "t.",
        "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream",
        "vapostproc",
        f"video/x-raw,format=BGRx,width={INFER_INPUT_WIDTH},height={INFER_INPUT_HEIGHT}",
        "appsink name=infer emit-signals=true drop=true max-buffers=1 sync=false",
    ])
    return " ! ".join(source_chain + ["tee name=t"]) + f" {display_branch} {infer_branch}"


def _on_infer_result(boxes, ts_ns: int) -> None:  # noqa: ANN001
    latest.set(boxes, ts_ns)


def _on_new_sample(sink: Gst.Element) -> Gst.FlowReturn:
    global detector
    sample = sink.emit("pull-sample")
    if sample is None or detector is None:
        return Gst.FlowReturn.OK
    buf = sample.get_buffer()
    caps = sample.get_caps()
    if buf is None or caps is None:
        return Gst.FlowReturn.OK
    width, height = _caps_wh(caps)
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        return Gst.FlowReturn.OK
    try:
        # BGRx is 4 bytes/px; at any width the row length is 4-byte aligned so
        # GStreamer adds no row padding — a flat reshape is safe.
        frame = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape(height, width, 4).copy()
    finally:
        buf.unmap(mapinfo)
    counts["samples"] += 1
    if detector.submit(frame, FrameMeta(display_width=display_size["w"], display_height=display_size["h"], submit_ns=time.perf_counter_ns())):
        counts["submitted"] += 1
    return Gst.FlowReturn.OK


def _on_draw(_overlay, cr, _timestamp: int, _duration: int) -> None:  # noqa: ANN001
    counts["draws"] += 1
    boxes, ts_ns = latest.get()
    if time.perf_counter_ns() - ts_ns > 200_000_000:
        return
    cr.set_source_rgb(0.0, 1.0, 0.0)
    cr.set_line_width(2.0)
    cr.select_font_face("sans")
    cr.set_font_size(16.0)
    for box in boxes:
        cr.rectangle(box.x1, box.y1, box.x2 - box.x1, box.y2 - box.y1)
        cr.stroke()
        cr.move_to(box.x1, max(12.0, box.y1 - 4.0))
        cr.show_text(f"{box.score:.2f}")


def _on_display_caps(_pad, info) -> Gst.PadProbeReturn:  # noqa: ANN001
    caps = _pad.get_current_caps()
    if caps is not None and caps.get_size() > 0:
        try:
            width, height = _caps_wh(caps)
            display_size["w"] = width
            display_size["h"] = height
        except Exception:
            pass
    return Gst.PadProbeReturn.OK


def _on_bus(_bus: Gst.Bus, msg: Gst.Message) -> bool:
    if msg.type == Gst.MessageType.ERROR:
        err, debug = msg.parse_error()
        log.error("[appsink] pipeline error: %s debug=%s", err, debug)
        if loop is not None:
            loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        log.info("[appsink] EOS")
        if loop is not None:
            loop.quit()
    return True


def _report() -> bool:
    now = time.monotonic()
    dt = now - last_report["t"]
    if dt <= 0:
        return True
    draw_fps = (counts["draws"] - last_report["draws"]) / dt
    infer_fps = (counts["submitted"] - last_report["submitted"]) / dt
    log.info("FpsCounter(appsink): display=%.2f fps infer_submit=%.2f fps samples=%s", draw_fps, infer_fps, counts["samples"])
    last_report.update({"t": now, "samples": counts["samples"], "submitted": counts["submitted"], "draws": counts["draws"]})
    return True


def _pinning_status() -> str:
    # Report the affinity/scheduler this process actually inherited from the
    # launcher's taskset/chrt prefix (env vars are the requested values).
    try:
        cores = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = []
    policy_names = {0: "OTHER", 1: "FIFO", 2: "RR", 3: "BATCH", 5: "IDLE", 6: "DEADLINE"}
    try:
        policy = policy_names.get(os.sched_getscheduler(0), "?")
        prio = os.sched_getparam(0).sched_priority
    except (AttributeError, OSError):
        policy, prio = "?", "?"
    cores_str = ",".join(str(c) for c in cores) if cores else "<all>"
    return f"cores={cores_str} sched={policy} priority={prio}"


def main() -> int:
    global detector, loop
    Gst.init(None)
    detector = OpenVINODetector(
        model_xml=IR_XML,
        device=DEVICE,
        threshold=THRESHOLD,
        iou_threshold=IOU_THRESHOLD,
        nireq=OV_NIREQ,
        on_result=_on_infer_result,
    )
    pipeline_str = _build_pipeline()
    log.info("[appsink] core-pinning: %s (requested PIPELINE_GST_CORES=%s PIPELINE_GST_RT_PRIORITY=%s)",
             _pinning_status(),
             os.environ.get("PIPELINE_GST_CORES", "").strip() or "<unset>",
             os.environ.get("PIPELINE_GST_RT_PRIORITY", "").strip() or "<unset>")
    log.info("[appsink] generated pipeline: %s", pipeline_str)
    pipeline = Gst.parse_launch(pipeline_str)
    infer = pipeline.get_by_name("infer")
    overlay = pipeline.get_by_name("overlay")
    if infer is None or overlay is None:
        raise RuntimeError("appsink pipeline missing infer or overlay element")
    infer.connect("new-sample", _on_new_sample)
    overlay.connect("draw", _on_draw)
    overlay_src = overlay.get_static_pad("src")
    if overlay_src is not None:
        overlay_src.add_probe(Gst.PadProbeType.BUFFER, _on_display_caps)

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", _on_bus)
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(1, _report)

    def _stop(_signum, _frame) -> None:  # noqa: ANN001
        if loop is not None:
            loop.quit()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())