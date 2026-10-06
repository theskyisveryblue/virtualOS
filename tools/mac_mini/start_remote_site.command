#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER_SCRIPT="${ROOT_DIR}/tools/virtualos_stream_server/server.py"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8899}"
TARGET_APP="${TARGET_APP:-Codex}"
LOCAL_URL="http://127.0.0.1:${PORT}/debug"

if [[ ! -f "${SERVER_SCRIPT}" ]]; then
  echo "Server script not found: ${SERVER_SCRIPT}" >&2
  exit 1
fi

if [[ "${HOST}" != "127.0.0.1" && "${HOST}" != "localhost" && "${HOST}" != "::1" && -z "${VOS_AUTH_TOKEN:-}" ]]; then
  echo "Set VOS_AUTH_TOKEN before starting the LAN remote site." >&2
  exit 1
fi

get_lan_ip() {
  local ip=""
  ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
  if [[ -n "${ip}" ]]; then
    printf '%s\n' "${ip}"
    return
  fi

  ip="$(ipconfig getifaddr en1 2>/dev/null || true)"
  if [[ -n "${ip}" ]]; then
    printf '%s\n' "${ip}"
  fi
}

cd "${ROOT_DIR}"

echo "==> Starting virtualOS remote site on ${HOST}:${PORT}"
echo "==> Default target app in UI: ${TARGET_APP}"
python3 "${SERVER_SCRIPT}" --host "${HOST}" --port "${PORT}" &
SERVER_PID=$!

cleanup() {
  kill "${SERVER_PID}" 2>/dev/null || true
  wait "${SERVER_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in {1..20}; do
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

LAN_IP="$(get_lan_ip || true)"

echo
echo "Local control page:"
echo "  ${LOCAL_URL}"

if [[ -n "${LAN_IP}" ]]; then
  echo "LAN control page:"
  echo "  http://${LAN_IP}:${PORT}/debug"
fi

echo
echo "Opening browser..."
open "${LOCAL_URL}"

echo
echo "Leave this Terminal window open while the remote site is running."
wait "${SERVER_PID}"
