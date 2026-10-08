#!/usr/bin/env bash
# run-local-test.sh — one command to run the offline relay integration test.
#
# Starts `wrangler dev --local` in an isolated, throwaway state dir, runs
# test_relay_local.sh against it, then tears everything down. No network, no
# Cloudflare account, no secrets. Use this in CI and before deploying the Worker.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8799}"
URL="http://127.0.0.1:$PORT"
HOST_TOKEN="hosttok"
WEB_TOKEN="webtok"
TTL_MS="2000"

# Locate wrangler: PATH first, then the npx cache (where it lives on this Mac).
WRANGLER="${WRANGLER:-}"
if [[ -z "$WRANGLER" ]]; then
  if command -v wrangler >/dev/null 2>&1; then
    WRANGLER="wrangler"
  else
    for c in "$HOME"/.npm/_npx/*/node_modules/wrangler/bin/wrangler.js; do
      [[ -f "$c" ]] && WRANGLER="node $c" && break
    done
  fi
fi
[[ -n "$WRANGLER" ]] || { echo "wrangler not found; set WRANGLER or install it" >&2; exit 1; }

STATE="$(mktemp -d)"
LOG="$(mktemp)"
cleanup() {
  [[ -n "${PID:-}" ]] && kill "$PID" 2>/dev/null
  wait 2>/dev/null
  rm -rf "$STATE" "$LOG"
}
trap cleanup EXIT

echo "starting local relay ($WRANGLER) on $URL ..."
(cd "$HERE" && $WRANGLER dev --local --port "$PORT" --persist-to "$STATE" \
  --var AUTH_TOKEN:"$HOST_TOKEN" --var WEB_TOKEN:"$WEB_TOKEN" --var INPUT_TTL_MS:"$TTL_MS" \
  > "$LOG" 2>&1) &
PID=$!

for i in $(seq 1 60); do
  curl -sf -m 2 "$URL/health" >/dev/null 2>&1 && break
  kill -0 "$PID" 2>/dev/null || { echo "wrangler exited early:"; cat "$LOG"; exit 1; }
  sleep 1
done
curl -sf -m 2 "$URL/health" >/dev/null 2>&1 || { echo "relay never became ready:"; tail -20 "$LOG"; exit 1; }

RELAY_LOCAL_URL="$URL" HOST_TOKEN="$HOST_TOKEN" WEB_TOKEN="$WEB_TOKEN" TTL_MS="$TTL_MS" \
  bash "$HERE/test_relay_local.sh"
rc=$?
exit $rc
