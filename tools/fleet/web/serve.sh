#!/usr/bin/env bash
# Serve the experimental virtualOS fleet web view and print the URLs.
#
#   tools/fleet/web/serve.sh [port]
#
# Env:
#   VOS_FLEET_MUX_URL   suggested supervisor/mux base URL to print (default :9300)
#   VOS_FLEET_WEB_HOST  bind host for the static server (default 127.0.0.1)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8080}"
HOST="${VOS_FLEET_WEB_HOST:-127.0.0.1}"
MUX="${VOS_FLEET_MUX_URL:-http://127.0.0.1:9300}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi

PAGE_URL="http://${HOST}:${PORT}/?supervisor=${MUX}"

cat <<EOF
virtualOS fleet web view (EXPERIMENTAL) — serving ${DIR}

  page URL:        http://${HOST}:${PORT}/
  with supervisor: ${PAGE_URL}
  suggested mux:   ${MUX}

Start the fleet mux in another terminal if it is not already running:

  tools/fleet/fleet.py serve --port 9300

Press Ctrl-C to stop the web server.
EOF

exec python3 -m http.server "${PORT}" --bind "${HOST}" --directory "${DIR}"
