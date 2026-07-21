#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${ROOT_DIR}/logs/latency"
LOG_FILE="${LOG_DIR}/popup.log"
TXT_FILE="${LOG_DIR}/popup.summary.txt"
JSON_FILE="${LOG_DIR}/popup.summary.json"
IMAGE="${IMAGE:-surgical-pipeline:dev}"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

if [[ -z "${DISPLAY:-}" ]]; then
  echo "DISPLAY is empty. Run from desktop GUI terminal."
  exit 1
fi

if ! xhost +local:docker >/dev/null 2>&1; then
  echo "xhost failed for DISPLAY=${DISPLAY}"
  exit 1
fi

RENDER_GID="$(getent group render | cut -d: -f3 || true)"
VIDEO_GID="$(getent group video | cut -d: -f3 || true)"

GROUP_ARGS=()
if [[ -n "${RENDER_GID}" ]]; then GROUP_ARGS+=(--group-add "${RENDER_GID}"); fi
if [[ -n "${VIDEO_GID}" ]]; then GROUP_ARGS+=(--group-add "${VIDEO_GID}"); fi

PIPELINE='gst-launch-1.0 -v \
  filesrc location=/videos/polyp_test.mp4 ! qtdemux ! h264parse ! vah264dec ! \
  identity sync=true ! \
  queue max-size-buffers=2 max-size-bytes=0 max-size-time=100000000 ! \
  gvadetect model=/models/yolo11n_polyp/best_openvino_model/best.xml device=GPU threshold=0.5 pre-process-backend=ie nireq=2 ! \
  queue max-size-buffers=4 max-size-bytes=0 max-size-time=100000000 leaky=downstream ! \
  gvawatermark ! gvafpscounter interval=1 ! \
  autovideosink sync=false'

echo "============================================================"
echo "MODE: POPUP_AUTOVIDEOSINK"
echo "IMAGE: ${IMAGE}"
echo "DISPLAY: ${DISPLAY}"
echo "LOG: ${LOG_FILE}"
echo "PIPELINE:"
echo "${PIPELINE}"
echo "============================================================"

docker run --rm --entrypoint bash --net=host \
  -e DISPLAY="${DISPLAY}" \
  -e XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}" \
  -e GST_TRACERS=latency \
  -e GST_DEBUG=GST_TRACER:7 \
  -e GST_DEBUG_NO_COLOR=1 \
  -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
  -v "${ROOT_DIR}/models:/models:ro" \
  -v "${ROOT_DIR}/videos:/videos:ro" \
  --device /dev/dri:/dev/dri \
  "${GROUP_ARGS[@]}" \
  "${IMAGE}" -lc "${PIPELINE}" \
  2> >(tee "${LOG_FILE}" >&2)

python3 "${ROOT_DIR}/scripts/parse_latency_log.py" "${LOG_FILE}" \
  --txt-out "${TXT_FILE}" \
  --json-out "${JSON_FILE}"

echo "Saved:"
echo "  ${LOG_FILE}"
echo "  ${TXT_FILE}"
echo "  ${JSON_FILE}"
