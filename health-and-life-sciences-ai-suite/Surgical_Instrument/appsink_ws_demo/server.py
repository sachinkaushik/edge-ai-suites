#!/usr/bin/env python3
"""appsink -> WebSocket video server (low-latency browser preview PoC).

Runs the polyp-detection GStreamer pipeline IN-PROCESS via python-gi, ending in
`jpegenc ! appsink`. Each encoded JPEG frame is pulled from appsink (in memory,
no disk) and broadcast to all connected WebSocket clients. A browser canvas
client (client.html) decodes each frame with createImageBitmap and draws it.

Why this is low latency vs the current file-polling UI:
  - no disk write/read (appsink hands JPEG bytes straight to Python)
  - push, not poll (frame sent the instant it is encoded)
  - canvas draw, not <img> re-render

Detection latency is unchanged: jpegenc/appsink run AFTER gvadetect.

Env:
  SOURCE   file|basler   (default file -> /videos/polyp_test.mp4)
  WS_PORT  websocket port (default 8090)
  DEVICE   GPU|CPU       (default GPU)
  QUALITY  jpeg quality  (default 80)
  LEAKY    1|0           (default 1 -> newest-frame-wins)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

try:
    import websockets
except ImportError:
    sys.stderr.write("ERROR: pip install websockets (run.sh does this)\n")
    sys.exit(2)

# Silence noisy handshake tracebacks from non-WebSocket probes hitting the port.
logging.getLogger("websockets").setLevel(logging.CRITICAL)

Gst.init(None)

SOURCE = os.environ.get("SOURCE", "file").lower()
WS_PORT = int(os.environ.get("WS_PORT", "8090"))
DEVICE = os.environ.get("DEVICE", "GPU")
QUALITY = int(os.environ.get("QUALITY", "80"))
LEAKY = os.environ.get("LEAKY", "1") == "1"
MODEL = "/models/yolo11n_polyp/best_openvino_model/best.xml"

_q = "queue max-size-buffers=1 max-size-bytes=0 max-size-time=16000000 leaky=downstream" \
    if LEAKY else "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0"

# Source segment. File plays paced (identity sync=true = real-time 60 fps).
if SOURCE == "file":
    SRC = (
        "filesrc location=/videos/polyp_test.mp4 ! qtdemux ! h264parse ! vah264dec ! "
        "video/x-raw(memory:VAMemory) ! identity sync=true"
    )
else:
    sys.stderr.write(f"ERROR: SOURCE={SOURCE!r} not supported in this PoC (use file)\n")
    sys.exit(2)

# gvadetect (GPU zero-copy) -> watermark -> download -> jpeg -> appsink.
PIPELINE = (
    f"{SRC} ! {_q} ! "
    f"gvadetect model={MODEL} device={DEVICE} threshold=0.5 "
    f"pre-process-backend=va-surface-sharing nireq=1 ie-config=PERFORMANCE_HINT=LATENCY ! "
    f"{_q} ! gvawatermark ! vapostproc ! video/x-raw ! "
    f"jpegenc quality={QUALITY} ! "
    "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
)

clients: set = set()
_latest = {"data": None}
_loop: asyncio.AbstractEventLoop | None = None
_new_frame: asyncio.Event | None = None
_count = {"n": 0}


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
            await asyncio.gather(
                *[c.send(data) for c in list(clients)], return_exceptions=True
            )


async def _handler(ws):
    clients.add(ws)
    peer = getattr(ws, "remote_address", "?")
    sys.stderr.write(f"[ws] client connected {peer} (total={len(clients)})\n")
    try:
        await ws.wait_closed()
    finally:
        clients.discard(ws)
        sys.stderr.write(f"[ws] client gone {peer} (total={len(clients)})\n")


def _run_glib_loop(pipeline):
    """Watch the bus for EOS/errors on a GLib main loop (background thread)."""
    ml = GLib.MainLoop()
    bus = pipeline.get_bus()

    def _on_msg(_bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
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


async def _stats():
    last = 0
    while True:
        await asyncio.sleep(5.0)
        fps = (_count["n"] - last) / 5.0
        last = _count["n"]
        sys.stderr.write(f"[stat] frames={_count['n']} ~{fps:.1f} fps  clients={len(clients)}\n")


async def main():
    global _loop, _new_frame
    _loop = asyncio.get_running_loop()
    _new_frame = asyncio.Event()
    asyncio.create_task(_broadcaster())
    asyncio.create_task(_stats())

    sys.stderr.write("[gst] pipeline:\n" + PIPELINE + "\n")
    pipeline = Gst.parse_launch(PIPELINE)
    sink = pipeline.get_by_name("sink")
    sink.connect("new-sample", _on_new_sample)

    threading.Thread(target=_run_glib_loop, args=(pipeline,), daemon=True).start()
    pipeline.set_state(Gst.State.PLAYING)
    sys.stderr.write(f"[ws] serving ws://0.0.0.0:{WS_PORT}\n")

    async with websockets.serve(_handler, "0.0.0.0", WS_PORT, max_size=None):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
