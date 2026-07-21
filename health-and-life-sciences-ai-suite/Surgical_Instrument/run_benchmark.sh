#!/usr/bin/env bash
set -euo pipefail

# Definitive model latency/throughput on the GPU using OpenVINO benchmark_app,
# isolated from GStreamer (no decode, no queues, no pacing). This is the number
# that model-level optimizations (INT8, input size, device) actually move.
#
# Runs two modes:
#   - latency    : single inference request, reports median/avg per-infer ms
#   - throughput : many requests in flight, reports FPS
#
# Override the model with MODEL=..., device with DEVICE=..., duration with T=...

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${ROOT_DIR}/logs/latency"
IMAGE="${IMAGE:-surgical-pipeline:dev}"
MODEL="${MODEL:-/models/yolo11n_polyp/best_openvino_model/best.xml}"
DEVICE="${DEVICE:-GPU}"
T="${T:-20}"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

RENDER_GID="$(getent group render | cut -d: -f3 || true)"
VIDEO_GID="$(getent group video  | cut -d: -f3 || true)"

GROUP_ARGS=()
if [[ -n "${RENDER_GID}" ]]; then GROUP_ARGS+=(--group-add "${RENDER_GID}"); fi
if [[ -n "${VIDEO_GID}" ]]; then GROUP_ARGS+=(--group-add "${VIDEO_GID}"); fi

run_mode() {
  local hint="$1" out="${LOG_DIR}/benchmark_${1}.log"
  echo "============================================================"
  echo "benchmark_app  hint=${hint}  device=${DEVICE}  model=${MODEL}"
  echo "log: ${out}"
  echo "============================================================"
  docker run --rm --entrypoint bash \
    -v "${ROOT_DIR}/models:/models:ro" \
    --device /dev/dri:/dev/dri \
    "${GROUP_ARGS[@]}" \
    "${IMAGE}" -lc \
    "benchmark_app -m '${MODEL}' -d '${DEVICE}' -hint ${hint} -t ${T}" \
    2>&1 | tee "${out}"
  echo
  echo "----- key results (${hint}) -----"
  grep -Ei 'Throughput|Median|Average|Count|Latency|Min|Max' "${out}" | tail -12
  echo
}

run_mode latency
run_mode throughput
