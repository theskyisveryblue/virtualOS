#!/usr/bin/env bash
# plan-status.sh — the truth scoreboard.
#
# Runs every measured lab and prints the whole state of the project as numbers,
# not claims. This is the "single command that prints the number" the plan
# promises. It is read-only against the stable planes and never starts a VM
# unless a check explicitly needs one.
#
#   tools/fleet/plan-status.sh            # full scoreboard
#   tools/fleet/plan-status.sh --quick    # skip anything slow/local-only
#
# It is RELAY-FIRST: the operator Mac can roam off the Mini's LAN and the cloud
# relay still carries the fleet truth. Only data that truly lives on the Mini
# (the delta experiment, the Linux console log, the self-test binary) needs SSH,
# and that probe runs once and degrades to "not reachable from here".
#
# Exit code is 0 even when a pillar is "blocked" — a blocked hard truth is a
# result, not a failure. The point is to see reality.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
QUICK="${1:-}"
HOST="$(hostname -s 2>/dev/null || echo local)"
MINI="${VOS_MINI_HOST:-mac-mini-ai}"

line() { printf '%-26s %s\n' "$1" "$2"; }
sep()  { printf -- '----------------------------------------------------------------\n'; }

# A read-only relay token. The host token lives on the Mini; the operator Mac
# only has the browser token, which is allowed to GET /fleet/status.
relay_token() {
  [[ -n "${AUTH_TOKEN:-}" ]] && printf '%s\n' "${AUTH_TOKEN}"
  [[ -n "${VOS_AUTH_TOKEN:-}" ]] && printf '%s\n' "${VOS_AUTH_TOKEN}"
  [[ -f "$HOME/.config/virtualos/browser-token" ]] && { cat "$HOME/.config/virtualos/browser-token"; printf '\n'; }
  return 0
}

relay_status_json() {
  local tok body
  while IFS= read -r tok; do
    [[ -z "$tok" ]] && continue
    body="$(curl -s -m 6 -H "Authorization: Bearer $tok" "$RELAY_URL/fleet/status" 2>/dev/null)"
    if printf '%s' "$body" | python3 -c 'import sys,json;json.load(sys.stdin)' >/dev/null 2>&1; then
      printf '%s' "$body"
      return 0
    fi
  done < <(relay_token)
  return 1
}

# Read one dotted field out of a JSON blob passed as the first argument.
json_get() {
  python3 -c '
import json, sys
try:
    node = json.loads(sys.argv[2])
    for key in sys.argv[1].split("."):
        node = node[int(key)] if key.isdigit() else node[key]
    print(node)
except Exception:
    pass
' "$1" "${2:-}" 2>/dev/null || true
}

if [[ -f "$HOME/.config/virtualos/relay.env" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.config/virtualos/relay.env"
fi
RELAY_BODY="$(relay_status_json 2>/dev/null || true)"

# The Mini may be off this host's LAN (operator roaming). Probe once, quietly.
mini_reachable() {
  [[ "$QUICK" == "--quick" ]] && return 1
  ssh -o BatchMode=yes -o ConnectTimeout=3 "$MINI" true >/dev/null 2>&1
}
MINI_UP=0; mini_reachable && MINI_UP=1
mini_run() { [[ "$MINI_UP" == "1" ]] || return 1; ssh -o BatchMode=yes -o ConnectTimeout=4 "$MINI" "$1" 2>/dev/null; }

echo "virtualOS plan-status  host=$HOST  $(date '+%Y-%m-%d %H:%M:%S')"
sep

# --- P0: is the live stack actually live? ---------------------------------
echo "[P0] live stack"
SUP_URL="${VOS_SUP_URL:-http://127.0.0.1:9225}"
sup_body="$(curl -s -m 3 "$SUP_URL/fleet/status" 2>/dev/null || true)"
sup_src=""
if ! printf '%s' "$sup_body" | python3 -c 'import sys,json;json.load(sys.stdin)' >/dev/null 2>&1; then
  sup_body=""
fi
if [[ -z "$sup_body" ]]; then
  sup_body="$(mini_run "curl -s -m 3 http://127.0.0.1:9225/fleet/status" || true)"
  [[ -n "$sup_body" ]] && sup_src=" (mini)"
fi
# Relay-first: the relay carries the fleet the host pushed. It is the truth that
# survives the operator leaving the Mini's LAN.
if [[ -z "$sup_body" && -n "$RELAY_BODY" ]]; then
  sup_body="$(printf '%s' "$RELAY_BODY" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["fleet"]))' 2>/dev/null || true)"
  [[ -n "$sup_body" ]] && sup_src=" (via relay)"
fi
if printf '%s' "$sup_body" | python3 -c 'import sys,json;json.load(sys.stdin)' >/dev/null 2>&1; then
  n="$(json_get count "$sup_body")"
  states="$(printf '%s' "$sup_body" | python3 -c 'import sys,json
d=json.load(sys.stdin)
print(",".join("%s:%s"%(i.get("name"),i.get("state")) for i in d.get("instances",[])))' 2>/dev/null || true)"
  line "supervisor" "up${sup_src} vms=${n:-0} ${states:+[$states]}"
else
  line "supervisor" "DOWN — run: runbook.sh live"
fi
if [[ -n "$RELAY_BODY" ]]; then
  q="$(printf '%s' "$RELAY_BODY" | python3 -c 'import sys,json;d=json.load(sys.stdin);l=d["liveness"];print(("live" if not l["isStale"] else "STALE"), "hb_age=%ss" % l["lastHeartbeatAgeSec"])' 2>/dev/null || echo "unparseable")"
  line "relay" "$q"
elif [[ -f "$HOME/.config/virtualos/relay.env" ]]; then
  line "relay" "unreachable or unauthorized at ${RELAY_URL:-?}"
else
  line "relay" "no relay.env"
fi

# --- P1: storage / compact fleet ------------------------------------------
echo; echo "[P1] storage"
[[ -f "$ROOT/tools/cloud_disk.py" ]] && line "disk plane" "present (cloud_disk.py)"
# Run the real storage self-test so this pillar prints a number, not a claim.
# Skipped under --quick because it does disk I/O.
if [[ "$QUICK" != "--quick" && -x "$ROOT/tools/computer-space.sh" ]]; then
  cd_json="$(bash "$ROOT/tools/computer-space.sh" --cloud-disk-self-test 2>/dev/null || true)"
  st="$(printf '%s' "$cd_json" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("cloud_disk_self_test","?"))' 2>/dev/null || echo "?")"
  line "storage self-test" "${st} (checksum+overlay+reconnect)"
fi
r2cfg="$HOME/.config/virtualos/r2-mac-config.json"
if [[ -f "$r2cfg" ]]; then
  line "r2 config" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["object_prefix"])' "$r2cfg" 2>/dev/null)"
fi
# The headline experiment result is produced where the clones live (the Mini).
delta_json="/private/tmp/vos-exp-shared-base/delta/delta.json"
if [[ ! -f "$delta_json" ]]; then
  delta_json="$(mini_run "cat $delta_json 2>/dev/null" || true)"
  delta_src=" [mini]"
fi
if [[ -n "$delta_json" ]]; then
  python3 - "$delta_json" "$delta_src" <<'PY' 2>/dev/null || line "shared base+delta" "unparseable"
import json, sys
d = json.loads(sys.argv[1])
print("%-26s delta=%.1f MB (%.2f%% of disk)%s" % ("shared base+delta", d["delta_bytes"]/1e6, d["delta_pct"], sys.argv[2]))
PY
elif [[ "$MINI_UP" == "0" ]]; then
  line "shared base+delta" "not reachable from here (Mini off-LAN)"
else
  line "shared base+delta" "not measured — run experiments/run.sh on the Mini"
fi

# --- P2: agency / guest input ---------------------------------------------
echo; echo "[P2] agency"
line "single-guest input" "$([[ -f "$ROOT/tools/virtualos_stream_server/inputctl.swift" ]] && echo 'inputctl present' || echo 'missing')"
# fleet input is wired when the relay exposes /fleet/poll + /fleet/input and a
# host poller long-polls it. Only the host token can drain the queue, so the
# operator reports the wiring and the Mini proves the flow.
if [[ -n "${RELAY_URL:-}" ]]; then
  pinfo="$(curl -s -m 6 -H "Authorization: Bearer ${AUTH_TOKEN:-${VOS_AUTH_TOKEN:-}}" "$RELAY_URL/fleet/poll" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("poll=ok" if d.get("ok") else "poll=host-token-only")' 2>/dev/null || echo "poll=unreachable")"
  line "fleet input" "wired (relay /fleet/poll + /fleet/input; $pinfo)"
else
  line "fleet input" "wired in worker+host; no relay.env to probe"
fi
line "offline brain" "$([[ -f "$ROOT/tools/fleet/brain.py" ]] && echo 'brain.py present (click/key/type)' || echo 'missing')"

# --- P3: breadth -----------------------------------------------------------
echo; echo "[P3] breadth"
# Linux guest boot leaves a version in its console log: the `Linux (none) <ver>`
# banner or the shell's osrelease echo (`<ver>-0-virt`). Accept either.
LINUX_REMOTE="tr -d '\\000' < ~/vos-fleet/linux/console.log 2>/dev/null | sed 's/\\r/\\n/g' | grep -aoE 'Linux \\(none\\) [0-9][^ ]*|[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-z-]*virt' | tail -1"
linux_log="$FLEET_HOME/linux/console.log"
kern=""
if [[ -f "$linux_log" ]]; then
  kern=$(tr -d '\000' < "$linux_log" 2>/dev/null | sed 's/\r/\n/g' | grep -aoE 'Linux \(none\) [0-9][^ ]*|[0-9]+\.[0-9]+\.[0-9]+-[0-9a-z-]*virt' | tail -1)
  linux_src=""
else
  kern="$(mini_run "$LINUX_REMOTE" || true)"
  linux_src=" [mini]"
fi
if [[ -n "$kern" ]]; then
  line "linux guest" "${kern}${linux_src}"
elif [[ "$MINI_UP" == "0" ]]; then
  line "linux guest" "not reachable from here (Mini off-LAN)"
else
  line "linux guest" "not booted this session"
fi
hyp="$(mini_run "tr -d '\\000' < ~/vos-fleet/linux/console.log 2>/dev/null | grep -am1 'HYP mode not available'" || true)"
[[ -n "$hyp" ]] && line "nested (VM-in-VM)" "BLOCKED: guest has no EL2/HYP"
line "windows" "no path"

# --- P6: discipline --------------------------------------------------------
echo; echo "[P6] discipline"
line "manifesto" "$(grep -c '^- \*\*' "$ROOT/MANIFESTO.md" 2>/dev/null) earned entries"
# The check count comes from the script itself so the scoreboard never drifts.
self_test="$FLEET_HOME/tools/fleet/self_test.sh"
[[ -x "$self_test" ]] || self_test="$ROOT/tools/fleet/self_test.sh"
if [[ -x "$self_test" ]]; then
  checks="$(grep -cE '^echo "\[[0-9]+/[0-9]+\]' "$self_test" 2>/dev/null || echo '?')"
  line "fleet self-test" "available (${checks} checks)"
elif [[ "$MINI_UP" == "1" ]]; then
  st="$(mini_run '[[ -x ~/vos-fleet/tools/fleet/self_test.sh ]] && echo "mini" || echo n/a' || echo n/a)"
  line "fleet self-test" "$st"
else
  line "fleet self-test" "not reachable from here (Mini off-LAN)"
fi

sep
echo "verdict: a blocked hard truth is a result. drive the next number down."
