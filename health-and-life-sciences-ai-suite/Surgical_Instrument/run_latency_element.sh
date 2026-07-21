#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${ROOT_DIR}/logs/latency"
LOG_FILE="${LOG_DIR}/element.log"
TXT_FILE="${LOG_DIR}/element.summary.txt"
JSON_FILE="${LOG_DIR}/element.summary.json"
IMAGE="${IMAGE:-surgical-pipeline:dev}"
ELEMENT="${ELEMENT:-gvadetect0}"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

RENDER_GID="$(getent group render | cut -d: -f3 || true)"
VIDEO_GID="$(getent group video  | cut -d: -f3 || true)"

GROUP_ARGS=()
if [[ -n "${RENDER_GID}" ]]; then GROUP_ARGS+=(--group-add "${RENDER_GID}"); fi
if [[ -n "${VIDEO_GID}" ]]; then GROUP_ARGS+=(--group-add "${VIDEO_GID}"); fi

# TRUE inference latency measurement.
#
# The whole-pipeline latency (flags=pipeline) is polluted by source pacing and
# queue residency, so it swings 11/60/79 ms depending only on how frames are
# fed. Here we use flags=element, which reports the per-element src->sink time
# for EACH element. Parsing the gvadetect element gives the actual inference
# latency per frame, independent of pacing/queueing.
#
# Data path is the optimized one (va-surface-sharing + LATENCY hint), blocking
# queues so every frame is processed.
PIPELINE='gst-launch-1.0 -v \
  filesrc location=/videos/polyp_test.mp4 ! qtdemux ! h264parse ! vah264dec ! \
  "video/x-raw(memory:VAMemory)" ! \
  identity eos-after=3000 ! \
  queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! \
  gvadetect model=/models/yolo11n_polyp/best_openvino_model/best.xml device=GPU threshold=0.5 \
    pre-process-backend=va-surface-sharing nireq=1 ie-config=PERFORMANCE_HINT=LATENCY ! \
  queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! \
  gvawatermark ! gvafpscounter interval=1 ! \
  fakesink sync=false async=false'

echo "============================================================"
echo "MODE: ELEMENT LATENCY (per-element, isolates gvadetect inference)"
echo "IMAGE: ${IMAGE}"
echo "ELEMENT: ${ELEMENT}"
echo "LOG: ${LOG_FILE}"
echo "PIPELINE:"
echo "${PIPELINE}"
echo "============================================================"

docker run --rm --entrypoint bash --net=host \
  -e 'GST_TRACERS=latency(flags=element)' \
  -e GST_DEBUG=GST_TRACER:7 \
  -e GST_DEBUG_NO_COLOR=1 \
  -v "${ROOT_DIR}/models:/models:ro" \
  -v "${ROOT_DIR}/videos:/videos:ro" \
  --device /dev/dri:/dev/dri \
  "${GROUP_ARGS[@]}" \
  "${IMAGE}" -lc "${PIPELINE}" \
  2> >(tee "${LOG_FILE}" >&2)

echo
echo "===== gvadetect inference latency ====="
python3 "${ROOT_DIR}/parse_latency_log.py" "${LOG_FILE}" \
  --element "${ELEMENT}" \
  --txt-out "${TXT_FILE}" \
  --json-out "${JSON_FILE}"

echo "Saved:"
echo "  ${LOG_FILE}"
echo "  ${TXT_FILE}"
echo "  ${JSON_FILE}"
