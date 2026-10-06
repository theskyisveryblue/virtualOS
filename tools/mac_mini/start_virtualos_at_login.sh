#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${HOME}/Library/Logs/virtualos-startup.log"

mkdir -p "$(dirname "$LOG_FILE")"

exec >>"$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting virtualOS VM"

export VOS_INSTALLED_APP="${VOS_INSTALLED_APP:-/Applications/virtualOS.app}"

while true; do
  if ! curl -fsS --max-time 2 http://127.0.0.1:8845/vm/health 2>/dev/null | jq -e '.ready == true and .state == "running"' >/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] VM unhealthy; running start.sh"
    if ! "$SCRIPT_DIR/start.sh"; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] start.sh failed; retrying in 30 seconds"
    fi
  fi
  sleep 30
done
