# Runtime Configuration

All runtime behavior is configured through `make up` variables (forwarded to
`docker compose` as environment variables) or CLI flags applied through the
`EXTRA` passthrough. There are no in-app UI controls — the display window only
renders detections and exits on **ESC**.

## Sources

Select the frame source with `SOURCE`.

| `SOURCE` | Argument variable | Notes |
|---|---|---|
| `camera` (alias `basler`) | `SERIAL` | Basler serial number; empty selects the first camera. Use `make list-cameras`. |
| `webcam` (alias `v4l2`) | `DEVICE_INDEX` | `/dev/videoN` index. |
| `file` | `SOURCE_ARG` | Container-side video path, e.g. `/videos/polyp_test.mp4`. |

Examples:

```bash
make up SOURCE=camera SERIAL=<SERIAL_NUMBER>
make up SOURCE=webcam DEVICE_INDEX=0
make up SOURCE=file   SOURCE_ARG=/videos/polyp_test.mp4
```

## Low-latency profile switch

| Param | Values | Effect |
|---|---|---|
| `LOWLATENCY` | `0` / `1` | Master switch. `1` sets `CAMERA_TRIGGER=vsync`, `FULLSCREEN=1`, `LATENCY_TRACE=1`. Individual knobs still override the profile. |

Recommended low-latency invocation on a 60 Hz panel:

```bash
make up LOWLATENCY=1 CAMERA_TRIGGER=vsync VSYNC_DIVISOR=2 SERIAL=<SERIAL_NUMBER>
```

## Capture knobs

| Param | Values | Effect |
|---|---|---|
| `CAMERA_TRIGGER` | `off` / `software` / `vsync` | `off` = free-run; `software` = expose just-in-time; `vsync` = phase-lock capture to display vblank (present-completion trigger). Requires the OpenGL presenter. |
| `VSYNC_DIVISOR` | integer | vsync only: capture every Nth vblank. `1` on 60 Hz; raise on higher-refresh panels. |
| `EXPOSURE_US` | microseconds | Fixed shutter time. Empty leaves the camera as-is. Lower = less latency and motion blur, but darker — validate illumination. |

## Display knobs

| Param | Values | Effect |
|---|---|---|
| `PRESENTER` | `auto` / `gl` / `cv2` | Display backend. `auto` picks GL when available. |
| `FULLSCREEN` | `0` / `1` | `1` = fullscreen OpenGL, unredirected (direct scanout, ~1-2 fewer frames). `0` = windowed. |
| `LATENCY_TRACE` | `0` / `1` | Emit per-stage latency CSV to stdout. |
| `HEADLESS` | `0` / `1` | `1` = no display window (log-only). |

## Inference knobs

| Param | Values | Effect |
|---|---|---|
| `DEVICE` | `GPU` / `CPU` / `NPU` | OpenVINO inference device. |
| `THRESHOLD` | `0.0`-`1.0` | Detection confidence cutoff. |
| `FRAME_SKIP` | integer | Run inference every Nth captured frame. Display still shows every captured frame. |
| `MODEL` | container path | OpenVINO IR (`best.xml`) inside the container. |

## Bind-mount paths

| Param | Default | Effect |
|---|---|---|
| `MODELS_DIR` | `../models` | Host folder mounted to `/models` (read-only). |
| `VIDEOS_DIR` | `../videos` | Host folder mounted to `/videos` (read/write). |

## Image source

| Param | Default | Effect |
|---|---|---|
| `REGISTRY` | `true` | `true` pulls the image from `REGISTRY_URL`. `false` builds locally from `docker/Dockerfile`. |
| `REGISTRY_URL` | `intel/` | Prefix used to resolve the pulled image. |
| `TAG` | `latest` | Image tag. |

## CLI flags via `EXTRA`

Any additional app flag can be forwarded through `EXTRA`. Common examples:

```bash
# Core pinning + real-time priority.
make up SOURCE=camera SERIAL=<SERIAL_NUMBER> \
  EXTRA="--cpu-capture 1 --cpu-inference 2 --cpu-display 3 --rt-priority 20"

# Cap capture rate (only when CAMERA_TRIGGER=off).
make up SOURCE=camera SERIAL=<SERIAL_NUMBER> EXTRA="--camera-fps 60"

# Headless run (no display window).
make up SOURCE=file SOURCE_ARG=/videos/polyp_test.mp4 EXTRA="--headless"
```

`--rt-priority > 0` uses `SCHED_FIFO` (needs `CAP_SYS_NICE`, granted by the
Makefile). It falls back to normal scheduling when not permitted.

## Full flag / env reference

| Flag | Env | Default | Purpose |
|---|---|---|---|
| `--source` | `SOURCE` | `file` | `file` / `v4l2` / `basler` |
| `--source-arg` | `SOURCE_ARG` | `/videos/polyp_test.mp4` | path / device index / camera serial |
| `--device` | `DEVICE` | `GPU` | `CPU` / `GPU` / `NPU` |
| `--model` | `MODEL` | `.../best.xml` | OpenVINO IR path |
| `--threshold` | `THRESHOLD` | `0.5` | detection confidence |
| `--iou` | `IOU` | `0.45` | NMS IoU |
| `--frame-skip` | `FRAME_SKIP` | `1` | infer every Nth frame |
| `--width` / `--height` | `WIDTH` / `HEIGHT` | `1280` / `720` | capture resolution |
| `--camera-fps` | `CAMERA_FPS` | `0` | Basler capture-rate cap (0 = free-running) |
| `--headless` | `HEADLESS` | off | no window (benchmark / server) |
| `--presenter` | `PRESENTER` | `auto` | `auto` / `gl` / `cv2` |
| `--fullscreen` | `FULLSCREEN` | off | GL fullscreen direct-scanout |
| `--camera-trigger` | `CAMERA_TRIGGER` | `off` | `off` / `software` / `vsync` |
| `--vsync-divisor` | `VSYNC_DIVISOR` | `1` | vsync: capture every Nth vblank |
| `--latency-trace` | `LATENCY_TRACE` | off | per-stage latency CSV |
| `--record` | `RECORD` | — | write annotated `.mp4` |
| `--display-scale` | `DISPLAY_SCALE` | `1.0` | window scale |
| `--detection-ttl-ms` | `DETECTION_TTL_MS` | `200` | how long a detection stays overlaid |
| `--no-loop` | `LOOP=0` | loop on | stop file at EOF instead of looping |
| `--exposure-us` / `--gain` | `EXPOSURE_US` / `GAIN` | camera as-is | Basler manual exposure / gain |

## Latency reality check

On a **60 Hz** monitor, photon-to-pixel is physically floored at ~one refresh
(~16 ms). Fullscreen direct-scanout removes the compositor's 1-2 frames but
cannot beat the panel. Single-digit ms requires a **144-240 Hz+ low-lag** panel
with the compositor off.

The CSV `trigger_to_display_ms` ends at frame hand-off and excludes
present → GPU → scanout + exposure, so it reads lower than an external
photon-to-pixel measurement rig. Use it for before/after comparison, not as an
absolute figure.
