#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8898}"
ROOT_URL="http://127.0.0.1:${PORT}"
SNAP_DIR="${2:-$(cd "$(dirname "$0")/.." && pwd)/vm_debug_snapshots}"

mkdir -p "$SNAP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"

curl -s "${ROOT_URL}/api/vm/status" > "${SNAP_DIR}/status-${TS}.json"
curl -s "${ROOT_URL}/api/vm/logs?limit=800" > "${SNAP_DIR}/logs-${TS}.json"

echo "Saved:"
echo "  ${SNAP_DIR}/status-${TS}.json"
echo "  ${SNAP_DIR}/logs-${TS}.json"
