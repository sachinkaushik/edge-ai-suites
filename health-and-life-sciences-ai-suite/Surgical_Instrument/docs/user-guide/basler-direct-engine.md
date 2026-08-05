# Basler Direct Engine (DL-Streamer-free) — `basler_engine.py`

An alternative Basler ingest path that runs **pypylon + OpenVINO directly** —
no GStreamer / DL Streamer — modelled on the customer's proven endoscopy POC.
It exists to sidestep the **in-process pylon ↔ OpenVINO-GPU contention** that
throttles the DL Streamer `gencamsrc` pipeline on some platforms (Arrow Lake-P
clients: ~11–20 FPS). See the root-cause analysis in the section below.

## What it is

[`pipeline/basler_engine.py`](../../pipeline/basler_engine.py) is a small,
multi-threaded engine:

| Thread | Job | Core / priority |
| --- | --- | --- |
| camera | pypylon grab (free-run, `LatestImageOnly`), BGR convert | `--cam-cpu`, SCHED_FIFO 85 |
| inference | OpenVINO GPU compiled model + YOLO decode + NMS | `--inf-cpu`, SCHED_FIFO 80 |
| output | draw boxes, optional OpenCV window | — |
| fps | prints `FpsCounter(...)` lines (same shape as DL Streamer) | — |

Key properties:

- **pypylon `26.2.1`** (vs the DL Streamer image's old pylon `7.5`).
- **Direct OpenVINO** (`GPU_DISABLE_WINOGRAD_CONVOLUTION=YES`, `NUM_STREAMS=1`,
  `PERFORMANCE_HINT=LATENCY`) — no Ultralytics wrapper.
- Camera and inference on **separate threads / dedicated cores**, so GPU
  inference cannot starve pylon acquisition (the crux of the DL Streamer stall).
- Feeds the launcher's existing `RollingLatency` (via `--emit-tracer`) and
  prints `FpsCounter` lines, so **`/latency`, `/health`, and log tooling are
  unchanged**.

## Why it helps

The DL Streamer path runs `gencamsrc` (pylon) and `gvadetect` (OpenVINO GPU) in
**one process**; on Arrow Lake-P the OpenVINO host threads starve pylon's USB
acquisition thread → the camera delivers ~11–20 FPS while the GPU sits idle.
Putting acquisition on its own thread/core (this engine) — or in its own
process (the `gencamsrc ! fdsink | fdsrc` split) — removes that contention.

## Measured (reference host, Core Ultra 7 265K + acA1920-150uc)

```
[inf] compiled on GPU input=[1, 3, 640, 640]
FpsCounter(...): total≈150 fps        # GPU inference throughput
latency: mean≈17.5 ms  p95≈19.4 ms
```

The camera holds full rate while inference runs — no starvation.

## How to run

### Via the app (recommended)

The engine is selected by `PIPELINE_BASLER_INGEST=engine`; everything else
(backend, UI, `/start /stop /health /latency`) is unchanged.

```bash
# rebuild the pipeline image (pypylon 26.2.1 + opencv + numpy)
docker compose build surgical-pipeline

# start the stack with the direct engine as the Basler path
make up SOURCE_KIND=basler SOURCE_ARG=<SERIAL> PIPELINE_GST_CORES=0-3 \
       PIPELINE_BASLER_INGEST=engine REGISTRY=false
```

Then the normal control-plane calls drive it:

```bash
curl -s -X POST http://localhost:8000/start \
  -H 'Content-Type: application/json' \
  -d '{"device":"GPU","source":{"kind":"basler","arg":"<SERIAL>"}}'

docker logs surgical-pipeline --since 30s | grep -E "FpsCounter|\[cam\]|\[inf\]" | tail
curl -s http://localhost:8000/latency | python3 -m json.tool
curl -s -X POST http://localhost:8000/stop
```

Confirm the engine (not gst-launch) is running:

```bash
docker exec surgical-pipeline sh -lc 'ps -o args= -C python3' | grep basler_engine
```

Flip back to the DL Streamer path by leaving `PIPELINE_BASLER_INGEST` unset
(defaults to `gencamsrc`).

### Standalone (benchmark / smoke test)

Runs headless over SSH — no monitor needed:

```bash
cd pipeline
sudo ../endoscopy-poc/.venv/bin/python basler_engine.py \
  --device GPU --resolution 1280,720 --duration 15 \
  --model ../models/yolo11n_polyp/best_openvino_model/best.xml
```

`sudo` enables SCHED_FIFO (the per-thread RT pinning); without it the engine
still runs with affinity-only. Add `--cam-cpu N --inf-cpu M` to place the
camera and inference on specific (ideally `isolcpus`-isolated) cores, and
`--display` for an OpenCV window.

## Options

| Flag / env | Default | Meaning |
| --- | --- | --- |
| `PIPELINE_BASLER_INGEST` | `gencamsrc` | `engine` selects this path |
| `--device` | `GPU` | `CPU` / `GPU` / `NPU` |
| `--resolution` | `1280,720` | camera + capture size |
| `--cam-cpu` / `--inf-cpu` | (from `PIPELINE_GST_CORES`) | dedicated cores for camera / inference |
| `--threshold` | `0.5` | detection confidence threshold |
| `--emit-tracer` | off | emit gst-format latency lines (launcher sets this) |
| `--display` | off | OpenCV overlay window (needs X) |

## How it fits the codebase

- **`surgical-pipeline` stays a service**, but this path uses no DL Streamer —
  it spawns `basler_engine.py` as the launcher-managed subprocess.
- **Latency** flows through the existing `RollingLatency` (the engine emits
  gst-format lines that `latency_tracer_sink.pump_stream` already parses).
- **Backend / UI** are unchanged — same HTTP contract.
- The `gencamsrc` DL Streamer path and the `basler_reader.py | fdsrc` split
  remain available.

## Caveats

- The image ships **both** pypylon 26.2.1 (engine) and the system pylon 7.5 SDK
  (gencamsrc). The launcher drops `GENICAM_GENTL64_PATH` for the engine so
  pypylon uses its bundled runtime. If you run engine-only, the
  `gst-plugin-pylon` / gencamsrc build can be removed to slim the image.
- For the true RT result, isolate cores on the host
  (`isolcpus=… nohz_full=… rcu_nocbs=… irqaffinity=0`) and run with `sudo` /
  `CAP_SYS_NICE` (already granted to `surgical-pipeline`).
- The OpenCV display path is best-effort; for headless/RDP use it falls back to
  metrics-only.

## Related

- Quickstart: [quickstart.md](quickstart.md)
- Engine: [pipeline/basler_engine.py](../../pipeline/basler_engine.py)
- Launcher: [pipeline/launcher.py](../../pipeline/launcher.py)
- Customer POC (reference): [endoscopy-poc/](../../endoscopy-poc/)
