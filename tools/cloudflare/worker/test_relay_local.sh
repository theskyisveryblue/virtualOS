#!/usr/bin/env bash
# test_relay_local.sh — exercise the relay Worker's fleet routes OFFLINE.
#
# Why: the Cloudflare API token is often unavailable, so we cannot deploy or hit
# the live Worker. `wrangler dev --local` runs miniflare with no network, so this
# is how we regression-test relay behaviour (auth roles, input queue, result
# round-trip, TTL expiry) before deploying.
#
# Usage:
#   # terminal 1 — start the local relay with test tokens and a tiny input TTL
#   cd tools/cloudflare/worker
#   npx --no-install wrangler dev --local --port 8799 \
#     --var AUTH_TOKEN:hosttok --var WEB_TOKEN:webtok --var INPUT_TTL_MS:2000
#   # terminal 2
#   tools/cloudflare/worker/test_relay_local.sh
#
# Env:
#   RELAY_LOCAL_URL (default http://127.0.0.1:8799)
#   HOST_TOKEN      (default hosttok)   — must match dev --var AUTH_TOKEN
#   WEB_TOKEN       (default webtok)    — must match dev --var WEB_TOKEN
#   TTL_MS          (default 2000)      — must match dev --var INPUT_TTL_MS
set -uo pipefail

URL="${RELAY_LOCAL_URL:-http://127.0.0.1:8799}"
HOST_TOKEN="${HOST_TOKEN:-hosttok}"
WEB_TOKEN="${WEB_TOKEN:-webtok}"
TTL_MS="${TTL_MS:-2000}"

pass=0 fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
check(){ [[ "$2" == "$3" ]] && ok "$1 ($2)" || bad "$1 (got '$2', want '$3')"; }

# code METHOD PATH TOKEN [body]  -> prints "<http_code>\t<body>"
req() {
  local method="$1" path="$2" token="$3" body="${4:-}"
  local args=(-s -m 10 -X "$method" -w $'\t%{http_code}')
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" -d "$body")
  curl "${args[@]}" "$URL$path"
}
code() { printf '%s' "$1" | awk -F$'\t' '{print $NF}'; }
body() { printf '%s' "$1" | awk -F$'\t' '{$NF="";sub(/\t$/,"")}1'; }
jget() { python3 -c 'import sys,json;print(json.load(sys.stdin).get(sys.argv[1],""))' "$2" <<<"$1" 2>/dev/null; }

echo "== relay local test @ $URL (ttl=${TTL_MS}ms) =="

# 0. server reachable
r="$(req GET /health "" 2>/dev/null)"
[[ "$(code "$r")" == "200" ]] || { echo "relay not reachable at $URL — start it first (see header)"; exit 1; }

# 0b. drain any commands restored from a previous run. DO state persists across
# restarts (that durability is the point), so the queue is not assumed empty.
for _ in $(seq 1 200); do
  c="$(jget "$(body "$(req GET /fleet/poll "$HOST_TOKEN")")" command)"
  [[ "$c" == "None" || -z "$c" ]] && break
done
echo "  ....  drained restored queue"

# 1. auth is required
r="$(req GET /fleet/status "")"
check "no token -> 401" "$(code "$r")" "401"

# 2. browser can read fleet status
r="$(req GET /fleet/status "$WEB_TOKEN")"
check "browser /fleet/status -> 200" "$(code "$r")" "200"
jget "$(body "$r")" liveness >/dev/null && ok "fleet status has liveness" || bad "fleet status missing liveness"

# 3. host-only routes denied to browser
r="$(req GET /fleet/poll "$WEB_TOKEN")"
check "browser /fleet/poll -> 403" "$(code "$r")" "403"

# 4. host poll never blocks; empty queue -> command null
r="$(req GET /fleet/poll "$HOST_TOKEN")"
check "host /fleet/poll -> 200" "$(code "$r")" "200"
[[ "$(jget "$(body "$r")" command)" == "None" || "$(jget "$(body "$r")" command)" == "" ]] \
  && ok "empty poll -> command null" || bad "empty poll returned a command"

# 5. vm-name validation. The browser route gate rejects a malformed name before
# it reaches the DO (403); the DO itself validates with 400 for the host token.
r="$(req POST "/fleet/input/bad%20name" "$HOST_TOKEN" '{"action":"move","nx":0.5,"ny":0.5}')"
check "invalid vm name -> 400" "$(code "$r")" "400"

# 6. browser queue an input
r="$(req POST /fleet/input/vm-a "$WEB_TOKEN" '{"action":"move","nx":0.25,"ny":0.75}')"
check "browser input -> 200" "$(code "$r")" "200"
ID="$(jget "$(body "$r")" id)"
[[ -n "$ID" ]] && ok "input returned id" || bad "input did not return id"

# 7. read back -> pending
r="$(req GET "/fleet/input/vm-a/$ID" "$WEB_TOKEN")"
check "input read -> 200" "$(code "$r")" "200"
check "input pending" "$(jget "$(body "$r")" status)" "pending"

# 8. host drains the queue and sees exactly this command
r="$(req GET /fleet/poll "$HOST_TOKEN")"
polled="$(jget "$(body "$r")" command)"
grep -q "$ID" <<<"$polled" && ok "host poll delivered the queued id" || bad "host poll did not deliver $ID (got $polled)"
pvm="$(python3 -c 'import sys,json;print(json.load(sys.stdin).get("command",{}).get("vm",""))' <<<"$(body "$r")" 2>/dev/null)"
check "polled command targets vm-a" "$pvm" "vm-a"

# 9. host posts the result; client sees completed
r="$(req POST "/fleet/input/vm-a/result/$ID" "$HOST_TOKEN" '{"ok":true,"status":200}')"
check "host result -> 200" "$(code "$r")" "200"
r="$(req GET "/fleet/input/vm-a/$ID" "$WEB_TOKEN")"
check "input completed" "$(jget "$(body "$r")" status)" "completed"

# 10. TTL: a never-completed input expires
r="$(req POST /fleet/input/vm-a "$WEB_TOKEN" '{"action":"key","key":"return"}')"
TID="$(jget "$(body "$r")" id)"
r="$(req GET "/fleet/input/vm-a/$TID" "$WEB_TOKEN")"
check "fresh input pending" "$(jget "$(body "$r")" status)" "pending"
wait_s=$(( TTL_MS / 1000 + 2 ))
echo "  ....  waiting ${wait_s}s for TTL expiry"
sleep "$wait_s"
r="$(req GET "/fleet/input/vm-a/$TID" "$WEB_TOKEN")"
check "stale input completed" "$(jget "$(body "$r")" status)" "completed"
exp="$(python3 -c 'import sys,json;print(json.load(sys.stdin).get("result",{}).get("status",""))' <<<"$(body "$r")" 2>/dev/null)"
check "stale input reports expired" "$exp" "expired"

echo "----------------------------------------------------------------"
echo "relay local: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
