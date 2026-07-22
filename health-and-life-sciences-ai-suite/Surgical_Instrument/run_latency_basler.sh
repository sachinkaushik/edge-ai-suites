#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# BEST PIPELINE (LIVE BASLER CAMERA)  -  Surgical Instrument polyp detection
# ============================================================================
# Same optimized compute path as run_latency.sh, but the source is a LIVE
# Basler USB3 camera instead of a file.
#
# Source bridge (mirrors production pipeline_string.py `basler` kind):
#   basler_reader.py (pypylon)  ->  stdout raw UYVY frames
#     | fdsrc fd=0 blocksize=W*H*2 do-timestamp=true
#     ! rawvideoparse format=yuy2 W H fps      (parse UYVY-as-yuy2, per DLS note)
#     ! vapostproc ! "video/x-raw(memory:VAMemory),format=NV12"   (-> GPU/NV12)
#     ! gvadetect  pre-process-backend=va-surface-sharing  (zero-copy)
#     ! gvawatermark ! gvafpscounter ! fakesink
#
# The camera is the clock (self-paced at fps), so NO identity sync=true is used
# -> the measured latency is genuine live end-to-end, with no pacing artifact.
#
# Camera access: the container needs USB. We mount /dev/bus/usb and allow the
# USB cgroup. If enumeration still fails, add --privileged (see FALLBACK below).
#
# Usage:
#   bash run_latency_basler.sh                 # first camera, 1080p60, 3000 frames
#   SERIAL=12345678 bash run_latency_basler.sh # pick a specific camera
#   W=1920 H=1080 FPS=60 FRAMES=1500 NIREQ=1 bash run_latency_basler.sh
# ============================================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${ROOT_DIR}/logs/latency"
LOG_FILE="${LOG_DIR}/basler.log"
TXT_FILE="${LOG_DIR}/basler.summary.txt"
IMAGE="${IMAGE:-surgical-pipeline:dev}"

W="${W:-1920}"
H="${H:-1080}"
FPS="${FPS:-60}"
FRAMES="${FRAMES:-3000}"
NIREQ="${NIREQ:-1}"
SERIAL="${SERIAL:-}"                 # empty = first detected camera
BLOCKSIZE=$(( W * H * 2 ))           # UYVY = 2 bytes/px

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

RENDER_GID="$(getent group render | cut -d: -f3 || true)"
VIDEO_GID="$(getent group video  | cut -d: -f3 || true)"

GROUP_ARGS=()
if [[ -n "${RENDER_GID}" ]]; then GROUP_ARGS+=(--group-add "${RENDER_GID}"); fi
if [[ -n "${VIDEO_GID}" ]]; then GROUP_ARGS+=(--group-add "${VIDEO_GID}"); fi

# Command that runs INSIDE the container: python bridge | gst-launch.
# Inner double-quotes around the VAMemory caps are escaped so the container's
# bash -lc keeps the parens literal.
CMD="python3 /opt/basler_reader.py ${SERIAL} --geometry ${W}x${H}@${FPS} --pixel-format uyvy \
  | gst-launch-1.0 \
    fdsrc fd=0 blocksize=${BLOCKSIZE} do-timestamp=true ! \
    rawvideoparse format=yuy2 width=${W} height=${H} framerate=${FPS}/1 ! \
    vapostproc ! \"video/x-raw(memory:VAMemory),format=NV12\" ! \
    identity eos-after=${FRAMES} ! \
    queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! \
    gvadetect model=/models/yolo11n_polyp/best_openvino_model/best.xml device=GPU threshold=0.5 \
      pre-process-backend=va-surface-sharing nireq=${NIREQ} ie-config=PERFORMANCE_HINT=LATENCY ! \
    queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! \
    gvawatermark ! gvafpscounter interval=1 ! \
    fakesink sync=false async=false"

echo "============================================================"
echo "MODE: BASLER LIVE (va-surface-sharing + LATENCY hint, self-paced)"
echo "IMAGE:   ${IMAGE}"
echo "CAMERA:  serial='${SERIAL:-<first>}'  ${W}x${H}@${FPS}  nireq=${NIREQ}"
echo "FRAMES:  ${FRAMES}"
echo "LOG:     ${LOG_FILE}"
echo "============================================================"
echo "${CMD}"
echo "============================================================"

# USB access for the Basler camera. FALLBACK: if pypylon cannot open the
# device, replace the two USB lines below with:  --privileged
docker run --rm --entrypoint bash --net=host \
  -e 'GST_TRACERS=latency(flags=element)' \
  -e GST_DEBUG=GST_TRACER:7 \
  -e GST_DEBUG_NO_COLOR=1 \
  -v /dev/bus/usb:/dev/bus/usb \
  --device-cgroup-rule='c 189:* rmw' \
  -v "${ROOT_DIR}/models:/models:ro" \
  --device /dev/dri:/dev/dri \
  "${GROUP_ARGS[@]}" \
  "${IMAGE}" -lc "${CMD}" \
  2> >(tee "${LOG_FILE}" >&2)

echo
echo "===== CRITICAL-PATH (live camera-to-screen) LATENCY ====="
python3 - "${LOG_FILE}" "${TXT_FILE}" <<'PY'
import re, sys, statistics

log, txt_out = sys.argv[1], sys.argv[2]
COMPUTE_PREFIXES = ("gvadetect", "gvawatermark", "gvafpscounter")
rec = re.compile(r"\selement-latency,.*element=\(string\)([^,]+),.*time=\(guint64\)(\d+)")
per_elem = {}
with open(log, errors="ignore") as f:
    for line in f:
        m = rec.search(line)
        if not m:
            continue
        name, t_ns = m.group(1), int(m.group(2))
        ms = t_ns / 1e6
        if ms > 60_000.0:          # drop guint64 underflow wraps
            continue
        per_elem.setdefault(name, []).append(ms)

if not per_elem:
    sys.exit("No element-latency records found (camera may not have started).")

lines = []
critical = 0.0
lines.append(f"{'element':<20} {'samples':>8} {'mean_ms':>9} {'p99_ms':>8}")
lines.append("-" * 48)
for name in sorted(per_elem):
    vals = sorted(per_elem[name])
    mean = statistics.mean(vals)
    p99 = vals[min(len(vals) - 1, int(0.99 * len(vals) + 0.5))]
    on_path = name.startswith(COMPUTE_PREFIXES)
    lines.append(f"{name:<20} {len(vals):>8} {mean:>9.3f} {p99:>8.3f}{' *' if on_path else ''}")
    if on_path:
        critical += mean
lines.append("-" * 48)
lines.append(f"{'CRITICAL PATH (sum of *)':<38} {critical:>9.3f} ms")

out = "\n".join(lines) + "\n"
print(out, end="")
open(txt_out, "w").write(out)
PY

echo "Saved:"
echo "  ${LOG_FILE}"
echo "  ${TXT_FILE}"
