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
# Exit code is 0 even when a pillar is "blocked" — a blocked hard truth is a
# result, not a failure. The point is to see reality.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
QUICK="${1:-}"
HOST="$(hostname -s 2>/dev/null || echo local)"

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

echo "virtualOS plan-status  host=$HOST  $(date '+%Y-%m-%d %H:%M:%S')"
sep

# --- P0: is the live stack actually live? ---------------------------------
echo "[P0] live stack"
SUP_URL="${VOS_SUP_URL:-http://127.0.0.1:9225}"
sup_tok="$(relay_token | head -1)"
# The supervisor may be local, or only reachable on the Mini over the fleet net.
sup_body="$(curl -s -m 3 "$SUP_URL/fleet/status" 2>/dev/null || true)"
if ! printf '%s' "$sup_body" | python3 -c 'import sys,json;json.load(sys.stdin)' >/dev/null 2>&1; then
  sup_body="$(ssh -o BatchMode=yes -o ConnectTimeout=4 mac-mini-ai \
    "curl -s -m 3 http://127.0.0.1:9225/fleet/status" 2>/dev/null || true)"
  sup_host=" (mini)"
fi
if printf '%s' "$sup_body" | python3 -c 'import sys,json;json.load(sys.stdin)' >/dev/null 2>&1; then
  n="$(printf '%s' "$sup_body" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("count",0))')"
  line "supervisor" "up${sup_host:-} (:9225) vms=$n"
else
  line "supervisor" "DOWN — run: runbook.sh live"
fi
if [[ -f "$HOME/.config/virtualos/relay.env" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.config/virtualos/relay.env"
  if body="$(relay_status_json)"; then
    q="$(printf '%s' "$body" | python3 -c 'import sys,json;d=json.load(sys.stdin);l=d["liveness"];print(("live" if not l["isStale"] else "STALE"), "hb_age=%ss" % l["lastHeartbeatAgeSec"])' 2>/dev/null || echo "unparseable")"
    line "relay" "$q"
  else
    line "relay" "unreachable or unauthorized at $RELAY_URL"
  fi
else
  line "relay" "no relay.env"
fi

# --- P1: storage / compact fleet ------------------------------------------
echo; echo "[P1] storage"
if [[ -f "$ROOT/tools/cloud_disk.py" ]]; then
  line "disk plane" "present (cloud_disk.py)"
fi
r2cfg="$HOME/.config/virtualos/r2-mac-config.json"
if [[ -f "$r2cfg" ]]; then
  line "r2 config" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["object_prefix"])' "$r2cfg" 2>/dev/null)"
fi
# the headline experiment result, if it has been run. It is produced on the host
# that owns the clones (the Mini); fall back to reading it there.
delta_json="/private/tmp/vos-exp-shared-base/delta/delta.json"
if [[ ! -f "$delta_json" ]] && command -v ssh >/dev/null 2>&1; then
  delta_json="$(ssh -o BatchMode=yes -o ConnectTimeout=4 mac-mini-ai \
    "cat $delta_json 2>/dev/null" 2>/dev/null || true)"
  if [[ -n "$delta_json" ]]; then
    python3 - "$delta_json" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print("%-26s delta=%.1f MB (%.2f%% of disk) [mini]" % ("shared base+delta", d["delta_bytes"]/1e6, d["delta_pct"]))
PY
  else
    line "shared base+delta" "not measured — run experiments/run.sh on the Mini"
  fi
elif [[ -f "$delta_json" ]]; then
  python3 - "$delta_json" <<'PY'
import json, sys
d = json.loads(open(sys.argv[1]).read())
print("%-26s delta=%.1f MB (%.2f%% of disk)" % ("shared base+delta", d["delta_bytes"]/1e6, d["delta_pct"]))
PY
else
  line "shared base+delta" "not measured this session — run experiments/run.sh"
fi

# --- P2: agency / guest input ---------------------------------------------
echo; echo "[P2] agency"
line "single-guest input" "$([[ -f "$ROOT/tools/virtualos_stream_server/inputctl.swift" ]] && echo 'inputctl present' || echo 'missing')"
# fleet input is wired when the relay exposes /fleet/poll + /fleet/input and a
# host poller is long-polling it with --control. Only the host token can drain
# the queue, so the operator Mac reports the wiring and the Mini proves the flow.
if [[ -f "$HOME/.config/virtualos/relay.env" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.config/virtualos/relay.env"
  pinfo="$(curl -s -m 6 -H "Authorization: Bearer ${AUTH_TOKEN:-${VOS_AUTH_TOKEN:-}}" "$RELAY_URL/fleet/poll" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("poll=ok" if d.get("ok") else "poll=host-token-only")' 2>/dev/null || echo "poll=unreachable")"
  line "fleet input" "wired (relay /fleet/poll + /fleet/input; $pinfo)"
else
  line "fleet input" "wired in worker+host; no relay.env to probe"
fi

# --- P3: breadth -----------------------------------------------------------
echo; echo "[P3] breadth"
# Linux guest boot leaves one of two signatures in the console log: the
# `Linux (none) <ver>` kernel banner, or the shell's `/proc/sys/kernel/osrelease`
# echo (`<ver>-0-virt`). Accept either; report the version, not "booted".
linux_log="$FLEET_HOME/linux/console.log"
linux_kern_sed="sed 's/\\r/\\n/g'"
linux_kern_grep="grep -aoE 'Linux \\(none\\) [0-9][^ .]*[^\\n]*|[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-z-]*virt' | tail -1"
if [[ -f "$linux_log" ]]; then
  kern=$(tr -d '\000' < "$linux_log" 2>/dev/null | sed 's/\r/\n/g' | grep -aoE 'Linux \(none\) [0-9][^ ]*|[0-9]+\.[0-9]+\.[0-9]+-[0-9a-z-]*virt' | tail -1)
  line "linux guest" "${kern:-log present, no version}"
else
  kern="$(ssh -o BatchMode=yes -o ConnectTimeout=4 mac-mini-ai \
    "tr -d '\\000' < ~/vos-fleet/linux/console.log 2>/dev/null | sed 's/\\r/\\n/g' | grep -aoE 'Linux \\(none\\) [0-9][^ ]*|[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-z-]*virt' | tail -1" 2>/dev/null || true)"
  if [[ -n "$kern" ]]; then
    line "linux guest" "$kern [mini]"
  else
    line "linux guest" "not booted this session"
  fi
fi
# honest nested truth, from the guest log if available
hyp="$(ssh -o BatchMode=yes -o ConnectTimeout=4 mac-mini-ai \
  "tr -d '\\000' < ~/vos-fleet/linux/console.log 2>/dev/null | grep -am1 'HYP mode not available'" 2>/dev/null || true)"
[[ -n "$hyp" ]] && line "nested (VM-in-VM)" "BLOCKED: guest has no EL2/HYP"
line "windows" "no path"

# --- P6: discipline --------------------------------------------------------
echo; echo "[P6] discipline"
line "manifesto" "$(grep -c '^- \*\*' "$ROOT/MANIFESTO.md" 2>/dev/null) earned entries"
if [[ -x "$FLEET_HOME/tools/fleet/self_test.sh" ]]; then
  line "fleet self-test" "available"
else
  st="$(ssh -o BatchMode=yes -o ConnectTimeout=4 mac-mini-ai \
    '[[ -x ~/vos-fleet/tools/fleet/self_test.sh ]] && echo mini || echo n/a' 2>/dev/null || echo n/a)"
  line "fleet self-test" "$st"
fi

sep
echo "verdict: a blocked hard truth is a result. drive the next number down."
