#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# BEST PIPELINE (DISPLAY)  -  Surgical Instrument polyp detection (GPU)
# ============================================================================
# Same finalized data path as run_latency.sh, but renders to a live window
# (autovideosink) instead of fakesink so you can WATCH the detections.
#
#   vah264dec -> VAMemory -> gvadetect(va-surface-sharing, LATENCY hint, nireq=2)
#             -> gvawatermark -> gvafpscounter -> autovideosink sync=true
#
# NOTE: with a display sink the whole-pipeline latency number is NOT meaningful
# (autovideosink presents at the display refresh / frame PTS). Use run_latency.sh
# for the measured critical-path latency; use THIS one for visual verification.
#
# Requires a desktop GUI session (X11). Run from the RDP/desktop terminal.
# ============================================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${ROOT_DIR}/logs/latency"
LOG_FILE="${LOG_DIR}/display.log"
TXT_FILE="${LOG_DIR}/display.summary.txt"
IMAGE="${IMAGE:-surgical-pipeline:dev}"
FRAMES="${FRAMES:-3000}"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

if [[ -z "${DISPLAY:-}" ]]; then
  echo "DISPLAY is empty. Run this from the desktop / RDP GUI terminal."
  exit 1
fi

if ! xhost +local:docker >/dev/null 2>&1; then
  echo "xhost failed for DISPLAY=${DISPLAY} (is this an X session?)"
  exit 1
fi

RENDER_GID="$(getent group render | cut -d: -f3 || true)"
VIDEO_GID="$(getent group video  | cut -d: -f3 || true)"

GROUP_ARGS=()
if [[ -n "${RENDER_GID}" ]]; then GROUP_ARGS+=(--group-add "${RENDER_GID}"); fi
if [[ -n "${VIDEO_GID}" ]]; then GROUP_ARGS+=(--group-add "${VIDEO_GID}"); fi

# autovideosink sync=true presents each frame at its PTS (real-time playback).
# No identity pacing needed here - the sink is the clock.
#
# The compute path keeps frames in GPU memory (va-surface-sharing + VAMemory),
# but an X11 display sink needs SYSTEM memory, so we download the surface with
# `vapostproc ! video/x-raw` before autovideosink (otherwise the sink can't
# negotiate VAMemory and the pipeline fails to link/preroll).
PIPELINE="gst-launch-1.0 -v \
  filesrc location=/videos/polyp_test.mp4 ! qtdemux ! h264parse ! vah264dec ! \
  \"video/x-raw(memory:VAMemory)\" ! \
  identity eos-after=${FRAMES} ! \
  queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! \
  gvadetect model=/models/yolo11n_polyp/best_openvino_model/best.xml device=GPU threshold=0.5 \
    pre-process-backend=va-surface-sharing nireq=2 ie-config=PERFORMANCE_HINT=LATENCY ! \
  queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! \
  gvawatermark ! gvafpscounter interval=1 ! \
  vapostproc ! \"video/x-raw\" ! videoconvert ! \
  autovideosink sync=true"

echo "============================================================"
echo "MODE: BEST + DISPLAY (autovideosink)"
echo "IMAGE:   ${IMAGE}"
echo "DISPLAY: ${DISPLAY}"
echo "FRAMES:  ${FRAMES}"
echo "LOG:     ${LOG_FILE}"
echo "============================================================"
echo "${PIPELINE}"
echo "============================================================"

docker run --rm --entrypoint bash --net=host \
  -e DISPLAY="${DISPLAY}" \
  -e XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}" \
  -e 'GST_TRACERS=latency(flags=element)' \
  -e GST_DEBUG=GST_TRACER:7 \
  -e GST_DEBUG_NO_COLOR=1 \
  -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
  -v "${ROOT_DIR}/models:/models:ro" \
  -v "${ROOT_DIR}/videos:/videos:ro" \
  --device /dev/dri:/dev/dri \
  "${GROUP_ARGS[@]}" \
  "${IMAGE}" -lc "${PIPELINE}" \
  2> >(tee "${LOG_FILE}" >&2)

echo
echo "===== CRITICAL-PATH (compute) LATENCY ====="
python3 - "${LOG_FILE}" "${TXT_FILE}" <<'PY'
import re, sys, statistics

log, txt_out = sys.argv[1], sys.argv[2]

# Compute elements on the critical path (inference + post). Source/decode/queue/
# identity/sink are excluded: they carry read-ahead + pacing/display, not compute.
COMPUTE_PREFIXES = ("gvadetect", "gvawatermark", "gvafpscounter")

rec = re.compile(r"\selement-latency,.*element=\(string\)([^,]+),.*time=\(guint64\)(\d+)")
# Drop guint64 underflow wraps (negative latency -> ~2^64): with a sync=true
# display sink some per-element deltas go negative and wrap to huge values.
# Any real element latency is well under 60 s.
MAX_MS = 60_000.0
per_elem = {}
with open(log, errors="ignore") as f:
    for line in f:
        m = rec.search(line)
        if not m:
            continue
        name, t_ns = m.group(1), int(m.group(2))
        ms = t_ns / 1e6
        if ms > MAX_MS:
            continue
        per_elem.setdefault(name, []).append(ms)

if not per_elem:
    sys.exit("No element-latency records found (need GST_TRACERS='latency(flags=element)').")

lines = []
critical = 0.0
lines.append(f"{'element':<20} {'samples':>8} {'mean_ms':>9} {'p99_ms':>8}")
lines.append("-" * 48)
for name in sorted(per_elem):
    vals = sorted(per_elem[name])
    mean = statistics.mean(vals)
    p99 = vals[min(len(vals) - 1, int(0.99 * len(vals) + 0.5))]
    on_path = name.startswith(COMPUTE_PREFIXES)
    mark = " *" if on_path else "  "
    lines.append(f"{name:<20} {len(vals):>8} {mean:>9.3f} {p99:>8.3f}{mark}")
    if on_path:
        critical += mean

lines.append("-" * 48)
lines.append(f"{'CRITICAL PATH (sum of *)':<38} {critical:>9.3f} ms")
lines.append("(* = compute elements summed for camera-to-screen latency;")
lines.append(" source/decode/queue/identity/sink excluded as pacing/display)")

out = "\n".join(lines) + "\n"
print(out, end="")
open(txt_out, "w").write(out)
PY

echo "Saved:"
echo "  ${LOG_FILE}"
echo "  ${TXT_FILE}"
