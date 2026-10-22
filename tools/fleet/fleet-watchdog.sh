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
# It refuses to thrash: a per-hour cap bounds how often it acts.
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
MAX_ACTIONS_PER_HOUR="${VOS_WD_MAX_ACTIONS:-10}"

log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }

load_relay() {
  [[ -f "$RELAY_ENV" ]] || return 1
  RELAY_URL="${RELAY_URL:-https://virtualos-relay.ai-c24.workers.dev}"
}

heartbeat_state() {
  # Prints "live", "stale", or nothing when the relay is unreachable. Uses the
  # relay's own staleness verdict so the host and the watchdog agree.
  python3 "$FLEET_TOOLS/fleet/relay_read.py" --token-file "$RELAY_ENV" 2>/dev/null \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); l=d["liveness"]
    age=l.get("lastHeartbeatAgeSec", -1)
    print("live" if (l.get("isStale") is False and type(age) is int and age >= 0) else "stale")
except Exception:
    pass' 2>/dev/null
}

supervisor_ok() { curl -s -m 4 "http://127.0.0.1:$SUP_PORT/fleet/status" >/dev/null 2>&1; }
vm_running()    { curl -s -m 4 "http://127.0.0.1:$SUP_PORT/fleet/status" 2>/dev/null | grep -q '"state":"running"'; }
agent_ok()      { pgrep -f "fleet_push.py .*--watch --control" >/dev/null 2>&1; }
guest_resources_ok() {
  bash "$FLEET_TOOLS/computer-resources.sh" --report \
    | python3 "$FLEET_TOOLS/fleet/watchdog_state.py" --guest-admission
}

rate_gate() {
  python3 "$FLEET_TOOLS/fleet/watchdog_state.py" "$FLEET_HOME" "$MAX_ACTIONS_PER_HOUR" \
    || { log "recovery deferred by rate limit or private state guard"; return 1; }
}

converge() {
  log "heartbeat stale — converging"
  if ! supervisor_ok; then
    guest_resources_ok || { log "resource guard deferred supervisor recovery"; return 1; }
    rate_gate || return 1
    log "supervisor down; running go-live.sh"
    bash "$FLEET_TOOLS/fleet/go-live.sh" "$NAME" 2>&1 | sed 's/^/    /' || true
  elif ! vm_running; then
    guest_resources_ok || { log "resource guard deferred VM start"; return 1; }
    rate_gate || return 1
    log "no VM running; starting $NAME"
    bash "$FLEET_TOOLS/fleet/runbook.sh" start "$NAME" 2>&1 | sed 's/^/    /' || true
  else
    rate_gate || return 1
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
      --token-file "$RELAY_ENV" >/dev/null 2>&1 ) || true
}

log "fleet-watchdog starting (interval ${CHECK_INTERVAL}s, max ${MAX_ACTIONS_PER_HOUR} actions/hr)"
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
