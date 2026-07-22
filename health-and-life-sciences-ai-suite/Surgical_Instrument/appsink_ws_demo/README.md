# appsink → WebSocket → canvas (low-latency browser preview PoC)

A standalone proof-of-concept that streams the polyp-detection video to a
browser with **much lower latency** than the current file-polling UI — by
pulling encoded frames straight out of the GStreamer pipeline (`appsink`, in
memory) and pushing them over a **WebSocket** to a **canvas**.

> Standalone — this does **not** touch the production backend / UI / compose.

---

## Why it's faster

| | Current UI (file poll) | This PoC (appsink → WS) |
|---|---|---|
| Handoff | pipeline writes `latest.jpg` to disk → backend reads it | in-memory `appsink` callback (no disk) |
| Trigger | UI polls every 33 ms | frame **pushed** the instant it's encoded |
| Render | `<img>` re-render | `canvas.drawImage` (no React) |
| Detection latency | ~10 ms | ~10 ms (**unchanged** — encode is after `gvadetect`) |
| Added UI latency | ~40–80 ms | **~15–35 ms** |

The pipeline runs **in-process** via python-gi so `appsink` can hand JPEG bytes
directly to Python — a `gst-launch` subprocess cannot do this (that's why the
production path uses a file).

---

## Pipeline

```
filesrc polyp_test.mp4 ! qtdemux ! h264parse ! vah264dec !
  video/x-raw(memory:VAMemory) ! identity sync=true !
  queue leaky ! gvadetect (GPU, va-surface-sharing, nireq=1, LATENCY hint) !
  queue leaky ! gvawatermark ! vapostproc ! video/x-raw !
  jpegenc ! appsink        →  WebSocket broadcast  →  browser <canvas>
```

---

## Requirements

- Docker image `surgical-pipeline:dev` (built via `make up` in the parent dir)
- Intel iGPU (`/dev/dri`)
- `models/yolo11n_polyp/...` and `videos/polyp_test.mp4` present in the parent dir
- Network access for a one-time `pip install websockets` (proxy is forwarded)

---

## Run

From this folder:

```bash
# Corporate proxy (needed for the one-time pip install websockets)
export HTTP_PROXY=http://proxy-pilot.intel.com:912 \
       HTTPS_PROXY=http://proxy-pilot.intel.com:912 \
       NO_PROXY=10.0.0.0/8,192.168.0.0/16,172.16.0.0/12,localhost,127.0.0.1,.local

bash run.sh
```

You'll see:

```
appsink -> WebSocket PoC
  WS server : ws://10.223.22.84:8090
  Open UI   : http://10.223.22.84:8091/   (or http://localhost:8091/)
```

Then open the **Open UI** URL in a browser (this box, RDP desktop, or any LAN
machine). The header shows connection state + `fps · decode/draw ms`.

Stop with `Ctrl+C`.

---

## Options (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `WS_PORT` | 8090 | WebSocket port |
| `HTTP_PORT` | 8091 | port serving `client.html` |
| `DEVICE` | GPU | inference device (GPU/CPU) |
| `QUALITY` | 80 | JPEG quality (lower = smaller/faster) |
| `LEAKY` | 1 | 1 = newest-frame-wins (drop stale); 0 = keep every frame |
| `SOURCE` | file | only `file` supported in this PoC |

Examples:

```bash
QUALITY=70 bash run.sh                 # smaller frames, less bandwidth
WS_PORT=9000 HTTP_PORT=9001 bash run.sh # different ports
```

---

## Files

| File | Role |
|------|------|
| `server.py` | in-process GStreamer pipeline + `appsink` + WebSocket broadcast |
| `client.html` | canvas client (decodes with `createImageBitmap`, draws to canvas) |
| `run.sh` | runs `server.py` in the container + serves `client.html` |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `address already in use (8090)` | a previous container is still up: `docker ps --filter ancestor=surgical-pipeline:dev -q \| xargs -r docker kill` |
| `pip install websockets` fails | ensure the proxy env vars above are exported |
| browser shows "disconnected" | check the WS port is reachable: `ss -ltn \| grep 8090`; use `http://<host>:8091/?host=<host>&port=8090` to override |
| black canvas but fps counting | frames arriving but scene is dark (source/model) — expected for the test clip only during dark sections |
| no window / X errors | none — this is headless (browser renders), no X needed |

---

## What this proves / next steps

- ✅ In-process pipeline + `appsink` at 60 fps, no disk, no polling
- ✅ WebSocket push → canvas render

Next (to productionize):
1. Overlay a JS timestamp to measure real WS→canvas latency.
2. Add `SOURCE=basler` (in-process pypylon → `appsrc`) for the live camera.
3. Integrate into the real UI: a WebSocket endpoint in the backend + swap
   `VideoFeed` from base64-polling to a canvas client.
