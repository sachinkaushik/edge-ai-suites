#!/usr/bin/env python3
"""In-process appsink -> WebSocket video server (UI streaming mode).

Used by launcher.py when UI_VIDEO=1. Builds the SAME detection pipeline as the
normal path (via pipeline_string.build(sink_mode="appsink")) but runs it
IN-PROCESS with python-gi so `appsink` can hand each JPEG frame straight to a
WebSocket broadcaster - no disk, no polling.

Supports both sources:
  file   : fully in-process (Gst.parse_launch).
  basler : basler_reader.py runs as a subprocess; its stdout fd is wired into
           the in-process `fdsrc` (the pipeline string starts with `fdsrc fd=0`,
           which we override to the subprocess pipe fd).

Env (same as launcher.py):
  VIDEO, IR_XML, THRESHOLD, TARGET_FPS, MQTT_HOST, MQTT_TOPIC, FRAME_DIR,
  SOURCE_KIND, SOURCE_ARG, PP_BACKEND, DETECTION_DEVICE/DEVICE
  PIPELINE_WS_PORT   (default 8091)  - WebSocket port the UI/nginx connects to
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

try:
    import websockets
except ImportError:
    sys.stderr.write("[ws] ERROR: python 'websockets' not installed (add to pipeline/Dockerfile)\n")
    sys.exit(2)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

try:
    from gstgva import VideoFrame  # DL Streamer per-frame metadata API
except Exception:  # noqa: BLE001
    VideoFrame = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt")
from pipeline_string import build  # noqa: E402

logging.getLogger("websockets").setLevel(logging.CRITICAL)
Gst.init(None)

VIDEO = os.environ.get("VIDEO", "/videos/polyp_test.mp4")
IR_XML = os.environ.get("IR_XML", "/models/yolo11n_polyp/best_openvino_model/best.xml")
THRESHOLD = float(os.environ.get("THRESHOLD", "0.5"))
TARGET_FPS = int(os.environ.get("TARGET_FPS", "60"))
MQTT_HOST = os.environ.get("MQTT_HOST", "surgical-mqtt")
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "surgical/detections")
SOURCE_KIND = os.environ.get("SOURCE_KIND", "file").lower()
SOURCE_ARG = os.environ.get("SOURCE_ARG", VIDEO)
PP_BACKEND = os.environ.get("PP_BACKEND", "va-surface-sharing").lower()
DEVICE = os.environ.get("DETECTION_DEVICE", os.environ.get("DEVICE", "GPU"))
if DEVICE.lower() in ("xpu", "gpu"):
    DEVICE = "GPU"
FRAME_DIR = os.environ.get("FRAME_DIR", "/frames")
WS_PORT = int(os.environ.get("PIPELINE_WS_PORT", "8091"))
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

clients: set = set()
_latest = {"data": None}
_loop: asyncio.AbstractEventLoop | None = None
_new_frame: asyncio.Event | None = None
_count = {"n": 0}
_lat_ns: list = []          # capture(fdsrc do-timestamp) -> appsink latency, ns
_pipeline = None
_basler_proc: subprocess.Popen | None = None
_mqtt = None


def _mqtt_connect():
    """Connect a paho client so we can publish detection JSON ourselves
    (replacing the in-pipeline gvapython MQTTPublisher tee)."""
    global _mqtt
    if mqtt is None or not MQTT_HOST or not MQTT_TOPIC:
        sys.stderr.write("[ws] MQTT publish disabled (no paho or host/topic)\n")
        return
    try:
        _mqtt = mqtt.Client(client_id="surgical-ws-video")
        _mqtt.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        _mqtt.loop_start()
        sys.stderr.write(f"[ws] MQTT publishing to {MQTT_HOST}:{MQTT_PORT} topic={MQTT_TOPIC}\n")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[ws] MQTT connect failed: {exc}\n")
        _mqtt = None


def _on_roi_probe(pad, info):
    """Pad probe on gvawatermark src: read the detection ROIs (attached by
    gvadetect) straight off the buffer and publish them to MQTT in the same
    `gvametaconvert format=json` schema the backend already expects - so we no
    longer need gvametaconvert in the pipeline."""
    if _mqtt is None or VideoFrame is None:
        return Gst.PadProbeReturn.OK
    buf = info.get_buffer()
    if buf is None:
        return Gst.PadProbeReturn.OK
    try:
        vf = VideoFrame(buf)
        # Frame resolution (for the normalised bounding_box) from the pad caps.
        w = h = 0
        caps = pad.get_current_caps()
        if caps is not None and caps.get_size() > 0:
            st = caps.get_structure(0)
            ok_w, w = st.get_int("width")
            ok_h, h = st.get_int("height")
        objects = []
        for roi in vf.regions():
            rx, ry, rw, rh = roi.rect()
            conf = roi.confidence()
            label = roi.label() or ""
            lid = roi.label_id()
            tid = roi.object_id()
            det = {
                "confidence": float(conf) if conf is not None else 0.0,
                "label": str(label),
                "label_id": int(lid) if lid is not None else 0,
            }
            if w and h:
                det["bounding_box"] = {
                    "x_min": rx / w, "y_min": ry / h,
                    "x_max": (rx + rw) / w, "y_max": (ry + rh) / h,
                }
            obj = {
                "detection": det,
                "x": int(rx), "y": int(ry), "w": int(rw), "h": int(rh),
                "region_id": int(tid) if tid else 0,
                "roi_type": str(label),
            }
            if tid:
                obj["id"] = int(tid)
            objects.append(obj)
        # Publish one message per frame (objects may be empty) so the backend's
        # per-frame counters stay accurate - matches gvametaconvert behaviour.
        payload = {
            "objects": objects,
            "resolution": {"width": int(w), "height": int(h)},
            "timestamp": int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else 0,
        }
        _mqtt.publish(MQTT_TOPIC, json.dumps(payload), qos=0)
    except Exception:  # noqa: BLE001
        pass
    return Gst.PadProbeReturn.OK


def _on_new_sample(sink):
    sample = sink.emit("pull-sample")
    if sample is None:
        return Gst.FlowReturn.OK
    buf = sample.get_buffer()
    ok, mi = buf.map(Gst.MapFlags.READ)
    if ok:
        data = bytes(mi.data)
        buf.unmap(mi)
        _count["n"] += 1
        # capture -> appsink latency: buffer PTS (fdsrc do-timestamp = running
        # time at grab) vs the pipeline clock's current running time.
        if _pipeline is not None and buf.pts != Gst.CLOCK_TIME_NONE:
            clk = _pipeline.get_clock()
            if clk is not None:
                now_rt = clk.get_time() - _pipeline.get_base_time()
                lat = now_rt - buf.pts
                if 0 <= lat < 1_000_000_000:  # sane (<1 s)
                    _lat_ns.append(lat)
                    if len(_lat_ns) > 3000:
                        del _lat_ns[: len(_lat_ns) - 3000]
        if _loop is not None:
            _loop.call_soon_threadsafe(_set_latest, data)
    return Gst.FlowReturn.OK


def _set_latest(data: bytes):
    _latest["data"] = data
    if _new_frame is not None:
        _new_frame.set()


async def _broadcaster():
    assert _new_frame is not None
    while True:
        await _new_frame.wait()
        _new_frame.clear()
        data = _latest["data"]
        if data and clients:
            await asyncio.gather(*[c.send(data) for c in list(clients)], return_exceptions=True)


async def _handler(ws):
    clients.add(ws)
    try:
        await ws.wait_closed()
    finally:
        clients.discard(ws)


async def _stats():
    last = 0
    while True:
        await asyncio.sleep(10.0)
        fps = (_count["n"] - last) / 10.0
        last = _count["n"]
        if _lat_ns:
            v = sorted(_lat_ns)
            n = len(v)
            mean = sum(v) / n / 1e6
            p50 = v[n // 2] / 1e6
            p99 = v[min(n - 1, int(0.99 * n))] / 1e6
            sys.stderr.write(
                f"[ws] frames={_count['n']} ~{fps:.1f} fps  "
                f"capture->appsink latency mean={mean:.1f} p50={p50:.1f} p99={p99:.1f} ms  "
                f"clients={len(clients)}\n"
            )
        else:
            sys.stderr.write(f"[ws] frames={_count['n']} ~{fps:.1f} fps clients={len(clients)}\n")


def _run_glib_loop(pipeline):
    ml = GLib.MainLoop()
    bus = pipeline.get_bus()

    def _on_msg(_bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            # File sources loop: seek back to start instead of ending.
            if SOURCE_KIND in ("file", "file_novasource"):
                pipeline.seek_simple(
                    Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0
                )
            else:
                sys.stderr.write("[gst] EOS\n")
                ml.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            sys.stderr.write(f"[gst] ERROR {err}: {dbg}\n")
            ml.quit()
        return True

    bus.add_signal_watch()
    bus.connect("message", _on_msg)
    ml.run()


def _build_pipeline():
    """Return (Gst.Pipeline, appsink). Wires the basler subprocess fd for basler."""
    global _basler_proc
    pipeline_str = build(
        source_kind=SOURCE_KIND,
        source_arg=SOURCE_ARG,
        ir_xml=IR_XML,
        device=DEVICE,
        threshold=THRESHOLD,
        target_fps=TARGET_FPS,
        mqtt_host=MQTT_HOST,
        mqtt_topic=MQTT_TOPIC,
        frame_path=str(Path(FRAME_DIR) / "latest.jpg"),
        pp_backend=PP_BACKEND,
        sink_mode="appsink",
    )
    sys.stderr.write("[gst] pipeline:\n" + pipeline_str + "\n")

    # pipeline_string.py double-quotes caps segments (e.g. the basler
    # `"video/x-raw(memory:VAMemory),format=NV12"`) so gst-launch's shell keeps
    # the parens literal. Gst.parse_launch (in-process, no shell) does NOT want
    # those quotes - the parens/commas parse fine unquoted - so strip them.
    pipeline = Gst.parse_launch(pipeline_str.replace('"', ""))

    if SOURCE_KIND == "basler":
        # Spawn basler_reader.py and feed its stdout into the in-process fdsrc
        # via an explicit OS pipe (robust fd handoff: fdsrc reads `r`,
        # basler_reader writes `w`). Passing a subprocess.PIPE fileno() is
        # unreliable because Python's BufferedReader sits in between.
        r_fd, w_fd = os.pipe()
        _basler_proc = subprocess.Popen(
            [
                "python3", "/opt/basler_reader.py", SOURCE_ARG,
                "--geometry", f"1920x1080@{TARGET_FPS}", "--pixel-format", "uyvy",
            ],
            stdout=w_fd,
        )
        os.close(w_fd)  # parent only reads; child now owns the write end
        fdsrc = pipeline.get_by_name("src") or _find_element(pipeline, "GstFdSrc")
        if fdsrc is None:
            raise RuntimeError("basler mode: no fdsrc element found in pipeline")
        fdsrc.set_property("fd", r_fd)

    sink = pipeline.get_by_name("video_sink")
    if sink is None:
        raise RuntimeError("no appsink named 'video_sink' in pipeline")
    sink.connect("new-sample", _on_new_sample)

    # Publish detection JSON to MQTT ourselves (the in-pipeline gvapython
    # MQTTPublisher tee is dropped in appsink mode). We read the ROI meta that
    # gvadetect attached, straight off the gvawatermark src pad - no
    # gvametaconvert needed.
    wm = pipeline.get_by_name("watermark")
    if wm is not None:
        srcpad = wm.get_static_pad("src")
        if srcpad is not None:
            srcpad.add_probe(Gst.PadProbeType.BUFFER, _on_roi_probe)
    return pipeline, sink


def _find_element(pipeline, type_name: str):
    it = pipeline.iterate_elements()
    while True:
        ok, el = it.next()
        if ok != Gst.IteratorResult.OK:
            break
        if type(el).__name__ == type_name or el.__gtype__.name == type_name:
            return el
    return None


async def main():
    global _loop, _new_frame
    _loop = asyncio.get_running_loop()
    _new_frame = asyncio.Event()
    asyncio.create_task(_broadcaster())
    asyncio.create_task(_stats())

    _mqtt_connect()
    global _pipeline
    pipeline, _ = _build_pipeline()
    _pipeline = pipeline
    threading.Thread(target=_run_glib_loop, args=(pipeline,), daemon=True).start()
    pipeline.set_state(Gst.State.PLAYING)
    sys.stderr.write(f"[ws] serving ws://0.0.0.0:{WS_PORT} (source={SOURCE_KIND} device={DEVICE})\n")

    async with websockets.serve(_handler, "0.0.0.0", WS_PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        if _basler_proc is not None:
            _basler_proc.terminate()
