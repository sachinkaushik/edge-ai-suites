#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# appsink -> WebSocket -> canvas  (low-latency browser preview PoC)
# ============================================================================
# Runs server.py inside the pipeline container: the polyp pipeline built
# in-process via python-gi, ending in jpegenc ! appsink, broadcasting each
# JPEG frame over a WebSocket. Open client.html in a browser to view.
#
# This is a standalone PoC - it does NOT touch the production backend/UI.
#
# Usage:
#   bash run.sh                 # file source, GPU, ws on :8090
#   WS_PORT=8090 QUALITY=80 bash run.sh
#
# Then open in a browser (on this box or the LAN):
#   http://<host>:8091/            (the script serves client.html on :8091)
# ============================================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # Surgical_Instrument
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"          # appsink_ws_demo
IMAGE="${IMAGE:-surgical-pipeline:dev}"
WS_PORT="${WS_PORT:-8090}"
HTTP_PORT="${HTTP_PORT:-8091}"
DEVICE="${DEVICE:-GPU}"
QUALITY="${QUALITY:-80}"
LEAKY="${LEAKY:-1}"

RENDER_GID="$(getent group render | cut -d: -f3 || true)"
VIDEO_GID="$(getent group video  | cut -d: -f3 || true)"
GROUP_ARGS=()
if [[ -n "${RENDER_GID}" ]]; then GROUP_ARGS+=(--group-add "${RENDER_GID}"); fi
if [[ -n "${VIDEO_GID}" ]]; then GROUP_ARGS+=(--group-add "${VIDEO_GID}"); fi

HOST_IP="$(hostname -I | awk '{print $1}')"

echo "============================================================"
echo "appsink -> WebSocket PoC"
echo "  WS server : ws://${HOST_IP}:${WS_PORT}"
echo "  Open UI   : http://${HOST_IP}:${HTTP_PORT}/   (or http://localhost:${HTTP_PORT}/)"
echo "  device=${DEVICE} quality=${QUALITY} leaky=${LEAKY}"
echo "============================================================"

# Serve client.html from the host (simple, no container needed for the page).
( cd "${HERE}" && python3 -m http.server "${HTTP_PORT}" >/dev/null 2>&1 ) &
HTTP_PID=$!
trap 'kill ${HTTP_PID} 2>/dev/null || true' EXIT

# Run the WS video server inside the pipeline container.
# pip installs websockets at runtime (proxy env forwarded for corporate nets).
docker run --rm --entrypoint bash --net=host \
  -e SOURCE=file -e WS_PORT="${WS_PORT}" -e DEVICE="${DEVICE}" \
  -e QUALITY="${QUALITY}" -e LEAKY="${LEAKY}" \
  -e HTTP_PROXY="${HTTP_PROXY:-}" -e HTTPS_PROXY="${HTTPS_PROXY:-}" \
  -e http_proxy="${HTTP_PROXY:-}" -e https_proxy="${HTTPS_PROXY:-}" \
  -e NO_PROXY="${NO_PROXY:-}" -e no_proxy="${NO_PROXY:-}" \
  -v "${ROOT_DIR}/models:/models:ro" \
  -v "${ROOT_DIR}/videos:/videos:ro" \
  -v "${HERE}/server.py:/opt/ws_server.py:ro" \
  --device /dev/dri:/dev/dri \
  "${GROUP_ARGS[@]}" \
  "${IMAGE}" -lc "pip install --quiet --disable-pip-version-check websockets >/dev/null 2>&1 || pip install --disable-pip-version-check websockets; python3 /opt/ws_server.py"
