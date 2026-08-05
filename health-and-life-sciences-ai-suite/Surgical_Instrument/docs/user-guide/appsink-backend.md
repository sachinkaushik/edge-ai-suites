# Appsink Backend Runbook

This document covers the `PIPELINE_BACKEND=appsink` implementation for the
Surgical Instrument pipeline service. It follows the Option C design in
`docs/user-guide/design.md`.

## What changed

The existing `gvadetect` backend runs camera capture and GPU inference inline in
one GStreamer pipeline. On Arrow Lake-P client systems, that shape can hold the
Basler path around 9-20 FPS because inference work back-pressures or starves
camera acquisition.

The new `appsink` backend keeps GStreamer capture and display, but moves model
inference into Python:

```text
source ! tee name=t
  t. ! display branch ! cairooverlay ! sink
  t. ! leaky infer branch ! appsink -> OpenVINO AsyncInferQueue
```

The inference branch is intentionally lossy:

- `queue leaky=downstream`
- `appsink drop=true max-buffers=1 sync=false`
- OpenVINO async inference skips frames when all requests are busy

This means display FPS should track the camera/source rate even when inference
cannot process every frame. Detection boxes are drawn by `cairooverlay` from the
latest completed inference result.

## Files added or changed

New pipeline files:

- `pipeline/appsink_infer.py` - GStreamer app using `gi`/`Gst`, `tee`,
  `appsink`, and `cairooverlay`.
- `pipeline/ov_detector.py` - OpenVINO `AsyncInferQueue`, preprocessing,
  YOLO decode, NMS, and display-space box mapping.
- `pipeline/overlay_state.py` - thread-safe latest detections store.

Integration changes:

- `pipeline/launcher.py` reads `PIPELINE_BACKEND` and spawns
  `python3 /opt/appsink_infer.py` when set to `appsink`.
- `pipeline/Dockerfile` installs PyGObject/cairo dependencies, verifies
  `cairooverlay`, and copies the new Python files into `/opt`.
- `docker-compose.yaml` passes through the new backend and inference knobs.
- `Makefile` passes the same knobs to compose.

## Build

From the app root:

```bash
cd /home/intel/sachin/edge-ai-suites/health-and-life-sciences-ai-suite/Surgical_Instrument

TAG=appsink-dev docker compose build surgical-pipeline
```

The build should include these checks:

```bash
docker run --rm --entrypoint gst-inspect-1.0 \
  intel/surgical-pipeline:appsink-dev cairooverlay appsink gvafpscounter

docker run --rm --device /dev/dri:/dev/dri --entrypoint sh \
  intel/surgical-pipeline:appsink-dev \
  -lc 'gst-inspect-1.0 vapostproc && gst-inspect-1.0 vah264dec'
```

`vapostproc` and `vah264dec` require `/dev/dri` to be visible; without it, the
VA plugin can report zero features.

## Run with live Basler

Use the new backend by setting `PIPELINE_BACKEND=appsink`.

```bash
make up REGISTRY=false \
  TAG=appsink-dev \
  PIPELINE_BACKEND=appsink \
  SOURCE_KIND=basler \
  SOURCE_ARG=40715749 \
  BASLER_PIXEL_FORMAT=ycbcr422_8 \
  PIPELINE_DISPLAY_VIEW=1 \
  PIPELINE_VIDEO_SINK=xvimagesink \
  PIPELINE_SINK_SYNC=false
```

Start the pipeline from the backend/API contract as usual:

```bash
curl -s -X POST http://localhost:8000/start \
  -H 'Content-Type: application/json' \
  -d '{"device":"GPU","source":{"kind":"basler","arg":"40715749"}}'
```

Stop it with:

```bash
curl -s -X POST http://localhost:8000/stop
```

## Run with file source

This is useful for graph validation without opening the Basler camera:

```bash
make up REGISTRY=false \
  TAG=appsink-dev \
  PIPELINE_BACKEND=appsink \
  SOURCE_KIND=file \
  SOURCE_ARG=/videos/polyp_test.mp4 \
  PIPELINE_DISPLAY_VIEW=0
```

Then start:

```bash
curl -s -X POST http://localhost:8000/start \
  -H 'Content-Type: application/json' \
  -d '{"device":"GPU","source":{"kind":"file","arg":"/videos/polyp_test.mp4"}}'
```

## Direct container smoke test

For a bounded file-source smoke test outside the Flask launcher:

```bash
docker run --rm --device /dev/dri:/dev/dri --entrypoint timeout \
  -e VIDEO=/videos/polyp_test.mp4 \
  -e SOURCE_KIND=file \
  -e SOURCE_ARG=/videos/polyp_test.mp4 \
  -e IR_XML=/models/yolo11n_polyp/best_openvino_model/best.xml \
  -e DETECTION_DEVICE=CPU \
  -e PIPELINE_DISPLAY_VIEW=0 \
  -v "$PWD/models:/models:ro" \
  -v "$PWD/videos:/videos:ro" \
  intel/surgical-pipeline:appsink-dev \
  12 python3 /opt/appsink_infer.py
```

This validates graph startup, callbacks, OpenVINO loading, and clean EOS. CPU
FPS from this smoke test is not the live Basler acceptance metric.

## Key runtime knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `PIPELINE_BACKEND` | `gvadetect` | Set to `appsink` to enable this backend. |
| `DETECTION_DEVICE` | `GPU` | OpenVINO device: `GPU`, `CPU`, or `NPU`. |
| `OV_NIREQ` | `4` | OpenVINO async request count. |
| `INFER_THRESHOLD` | `0.5` | Detection confidence threshold. |
| `INFER_IOU` | `0.45` | NMS IoU threshold. |
| `PIPELINE_DISPLAY_VIEW` | `0` | Set `1` for a live popup sink. |
| `PIPELINE_VIDEO_SINK` | `autovideosink` | Use `xvimagesink` on the validated desktop path. |
| `PIPELINE_SINK_SYNC` | source-based | Use `false` for live Basler display testing. |
| `BASLER_PIXEL_FORMAT` | `bayerbggr` | Use `ycbcr422_8` for the optimized Basler path. |

## How to verify the backend is active

Check logs:

```bash
docker logs surgical-pipeline --since 30s | grep -E "appsink|FpsCounter"
```

Expected markers:

```text
[appsink] OpenVINO compiled device=GPU input=[1, 3, 640, 640] nireq=4
[appsink] generated pipeline: ... appsink name=infer ... cairooverlay name=overlay ...
FpsCounter(appsink): display=... fps infer_submit=... fps samples=...
```

Also confirm the supervised process is Python, not `gst-launch`:

```bash
docker exec surgical-pipeline sh -lc 'ps -eo pid,args | grep -E "appsink_infer|gst-launch" | grep -v grep'
```

For the appsink backend, you should see `python3 /opt/appsink_infer.py`.

## Acceptance test

For live Basler on the client machine:

1. Run with `PIPELINE_BACKEND=appsink`, `SOURCE_KIND=basler`, and the client
   camera serial.
2. Confirm display FPS from `gvafpscounter` and `FpsCounter(appsink)` is steady
   near the target camera rate.
3. Confirm no periodic one-second stalls.
4. Confirm detection boxes appear and lag by no more than about 1-2 frames.
5. Compare box quality against the old `gvadetect` backend on the same scene or
   clip.

The primary pass/fail metric is live displayed FPS. Inference FPS can be lower
because the infer branch is allowed to drop frames.

## Troubleshooting

### `no element "vapostproc"` or `no element "vah264dec"`

Run with `/dev/dri` mounted. The VA plugin may show zero features without the
GPU render device:

```bash
docker run --rm --device /dev/dri:/dev/dri --entrypoint gst-inspect-1.0 \
  intel/surgical-pipeline:appsink-dev vapostproc
```

### `could not link vapostproc to infer` with RGB caps

The infer branch must convert through system memory:

```text
vapostproc ! video/x-raw,format=BGRx,width=640,height=640 ! videoconvert ! video/x-raw,format=RGB,width=640,height=640 ! appsink
```

Do not request `RGB` directly from `vapostproc` in this image.

### No popup window

Use `PIPELINE_DISPLAY_VIEW=1`, pass through `DISPLAY`, and mount
`/tmp/.X11-unix`. On the validated Basler desktop path, prefer:

```bash
PIPELINE_VIDEO_SINK=xvimagesink PIPELINE_SINK_SYNC=false
```

For headless validation, keep `PIPELINE_DISPLAY_VIEW=0`; the backend still runs
with `fakesink`.

### Boxes are missing or inaccurate

Current preprocessing uses stretch resize to `640x640` on the infer branch and
maps detections back by simple scale. If detection quality is worse than
`gvadetect`, the next change should be a letterbox mode in `ov_detector.py` so
preprocessing matches the exported model more closely.

### Display FPS is still low

Check that the infer branch is not back-pressuring the graph:

```text
queue max-size-buffers=1 leaky=downstream ! ... ! appsink drop=true max-buffers=1 sync=false
```

Then compare:

- `FpsCounter(appsink): display=...`
- `infer_submit=...`
- `samples=...`

If `display` is low but `infer_submit` is also low, investigate source/camera
delivery. If `display` is high and `infer_submit` is lower, the backend is doing
the intended decoupling.

## Rollback

Unset the backend switch or set it explicitly to the old path:

```bash
make up REGISTRY=false PIPELINE_BACKEND=gvadetect
```

The original `pipeline_string.py` / `gst-launch-1.0` path remains available.