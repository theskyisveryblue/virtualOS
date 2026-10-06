#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${HOME}/.config/virtualos/remote_host.env"
LOG_FILE="${HOME}/Library/Logs/virtualos-remote-host.log"
SERVER_SCRIPT="${REPO_DIR}/tools/virtualos_stream_server/server.py"

mkdir -p "$(dirname "$ENV_FILE")"
mkdir -p "$(dirname "$LOG_FILE")"

exec >>"$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting virtualOS remote host"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

HOST="${VOS_REMOTE_HOST:-127.0.0.1}"
PORT="${VOS_REMOTE_PORT:-8899}"
AUTH_TOKEN="${VOS_AUTH_TOKEN:-}"

cd "$REPO_DIR"

ARGS=(--host "$HOST" --port "$PORT")
if [[ -n "$AUTH_TOKEN" ]]; then
  ARGS+=(--auth-token "$AUTH_TOKEN")
fi

exec python3 "$SERVER_SCRIPT" "${ARGS[@]}"
