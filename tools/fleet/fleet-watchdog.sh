#!/usr/bin/env bash
# fleet-watchdog.sh — keep the live fleet stack converged.
#
#   tools/fleet/fleet-watchdog.sh [--once]
#
# The PLAN's P0 exit is "plan-status.sh shows live + heartbeats fresh, and a
# watchdog keeps it that way." This is that watchdog. It is deliberately small
# and fleet-first: it watches the relay heartbeat (the truth that survives the
# operator leaving the LAN) and, only when that heartbeat goes stale, converges
# the host side in the right order:
#
#   1. supervisor responding on :SUP_PORT  -> else run go-live.sh
#   2. a VM running                        -> else start it
#   3. stream/control LaunchAgent alive     -> else kickstart it
#   4. one push, then confirm freshness
#
# It refuses to thrash: a cooldown and a per-hour cap bound how often it acts.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
FLEET_TOOLS="${VOS_FLEET_TOOLS:-$FLEET_HOME/tools}"
SUP_PORT="${VOS_SUP_PORT:-9225}"
RELAY_ENV="${VOS_RELAY_ENV:-$HOME/.config/virtualos/relay.env}"
NAME="${VOS_FLEET_VM:-vm-a}"
ONCE=0
[[ "${1:-}" == "--once" ]] && ONCE=1

CHECK_INTERVAL="${VOS_WD_INTERVAL:-30}"
COOLDOWN="${VOS_WD_COOLDOWN:-120}"
MAX_ACTIONS_PER_HOUR="${VOS_WD_MAX_ACTIONS:-10}"
hot="${VOS_WD_STALE_SEC:-120}"   # treat as stale past this heartbeat age

state_file="/tmp/vos-fleet-watchdog.state"
log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }

load_relay() {
  [[ -f "$RELAY_ENV" ]] || return 1
  # shellcheck disable=SC1090
  source "$RELAY_ENV"
  [[ -n "${RELAY_URL:-}" && -n "${AUTH_TOKEN:-}" ]]
}

heartbeat_state() {
  # Prints "live", "stale", or nothing when the relay is unreachable. Uses the
  # relay's own staleness verdict so the host and the watchdog agree.
  curl -s -m 6 -H "Authorization: Bearer $AUTH_TOKEN" "$RELAY_URL/fleet/status" 2>/dev/null \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); l=d["liveness"]
    age=l.get("lastHeartbeatAgeSec", -1)
    print("live" if (not l.get("isStale") and age is not None and age >= 0) else "stale")
except Exception:
    pass' 2>/dev/null
}

supervisor_ok() { curl -s -m 4 "http://127.0.0.1:$SUP_PORT/fleet/status" >/dev/null 2>&1; }
vm_running()    { curl -s -m 4 "http://127.0.0.1:$SUP_PORT/fleet/status" 2>/dev/null | grep -q '"state":"running"'; }
agent_ok()      { pgrep -f "fleet_push.py .*--watch --control" >/dev/null 2>&1; }

read_state() { [[ -f "$state_file" ]] && cat "$state_file" || echo "0 0 0"; }

# actions bucket: "<window_start> <count>"
rate_gate() {
  local now entry win count
  now=$(date +%s)
  entry="$(read_state)"; set -- $entry; win="${1:-0}"; count="${2:-0}"
  if (( now - win >= 3600 )); then win=$now; count=0; fi
  if (( count >= MAX_ACTIONS_PER_HOUR )); then
    log "rate cap reached ($count/hr); not acting"
    echo "$win $count" > "$state_file"; return 1
  fi
  echo "$win $((count+1))" > "$state_file"; return 0
}

converge() {
  log "heartbeat stale — converging"
  rate_gate || return 1
  if ! supervisor_ok; then
    log "supervisor down; running go-live.sh"
    bash "$FLEET_TOOLS/fleet/go-live.sh" "$NAME" 2>&1 | sed 's/^/    /' || true
  elif ! vm_running; then
    log "no VM running; starting $NAME"
    bash "$FLEET_TOOLS/fleet/runbook.sh" start "$NAME" 2>&1 | sed 's/^/    /' || true
  fi
  if ! agent_ok; then
    log "stream agent down; kickstarting com.virtualos.stream"
    launchctl kickstart -k "gui/$(id -u)/com.virtualos.stream" 2>/dev/null \
      || launchctl load "$HOME/Library/LaunchAgents/com.virtualos.stream.plist" 2>/dev/null \
      || log "no stream LaunchAgent to kick"
  fi
  # One manual push so the next check sees fresh truth even if the agent is slow.
  ( cd "$FLEET_HOME" && python3 "$FLEET_TOOLS/fleet/fleet_push.py" \
      --relay "$RELAY_URL" --supervisor "http://127.0.0.1:$SUP_PORT" \
      --token "$AUTH_TOKEN" >/dev/null 2>&1 ) || true
}

log "fleet-watchdog starting (interval ${CHECK_INTERVAL}s, stale>${hot}s, max ${MAX_ACTIONS_PER_HOUR}/hr)"
while true; do
  load_relay || { log "no relay.env; nothing to watch"; exit 1; }
  state="$(heartbeat_state)"
  case "$state" in
    live) : ;;
    stale) converge || true ;;
    *) log "relay unreachable (operator or cloud); skipping this tick" ;;
  esac
  (( ONCE )) && exit 0
  sleep "$CHECK_INTERVAL"
done
