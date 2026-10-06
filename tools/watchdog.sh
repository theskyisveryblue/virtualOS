#!/usr/bin/env bash
# watchdog.sh — self-healing monitor for virtualOS
#
# Runs in a loop, checks system health, and automatically fixes problems.
# Learns from failures and adapts intervals.
#
# Usage:
#   VOS_VM_PASSWORD=xxx ./tools/watchdog.sh
#   VOS_VM_PASSWORD=xxx RELAY_URL=... AUTH_TOKEN=... ./tools/watchdog.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VOSCTL="$REPO_DIR/tools/vosctl"
STREAM_SERVER="${STREAM_SERVER:-http://127.0.0.1:8899}"
STREAM_SERVER_SCRIPT="$REPO_DIR/tools/virtualos_stream_server/server.py"
LOG_FILE="$REPO_DIR/tools/watchdog.log"
STATE_FILE="/tmp/vos_watchdog_state.json"

# Adaptive intervals (seconds)
CHECK_INTERVAL=15
MIN_INTERVAL=10
MAX_INTERVAL=120
CONSECUTIVE_OK=0
CONSECUTIVE_FAIL=0
TOTAL_FIXES=0
TOTAL_CHECKS=0
STARTED_AT=$(date +%s)

# Limits — don't restart endlessly
MAX_VM_RESTARTS_PER_HOUR=5
MAX_STREAM_RESTARTS_PER_HOUR=10
vm_restarts_this_hour=0
stream_restarts_this_hour=0
hour_start=$(date +%s)

# ── colors ────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
DIM='\033[0;90m'
NC='\033[0m'

log()  { local msg="[$(date '+%H:%M:%S')] $*"; echo -e "${DIM}$msg${NC}"; echo "$msg" >> "$LOG_FILE"; }
ok()   { local msg="[$(date '+%H:%M:%S')] OK: $*"; echo -e "${GREEN}$msg${NC}"; echo "$msg" >> "$LOG_FILE"; }
warn() { local msg="[$(date '+%H:%M:%S')] WARN: $*"; echo -e "${YELLOW}$msg${NC}"; echo "$msg" >> "$LOG_FILE"; }
fix()  { local msg="[$(date '+%H:%M:%S')] FIX: $*"; echo -e "${CYAN}$msg${NC}"; echo "$msg" >> "$LOG_FILE"; }
err()  { local msg="[$(date '+%H:%M:%S')] ERR: $*"; echo -e "${RED}$msg${NC}"; echo "$msg" >> "$LOG_FILE"; }

# ── helpers ───────────────────────────────────────────────────────────

app_running()    { pgrep -x virtualOS >/dev/null 2>&1; }
stream_running() { curl -sf --connect-timeout 1 --max-time 2 "$STREAM_SERVER/health" >/dev/null 2>&1; }

vm_window_count() {
  osascript -e 'tell application "System Events" to tell process "virtualOS" to count of windows' 2>/dev/null || echo "0"
}

vm_running() { app_running && (( $(vm_window_count) > 1 )); }

vm_responsive() {
  # Check if VM is actually responding (not frozen) by checking if frame capture works
  if stream_running; then
    local http_code
    http_code=$(curl -sf --connect-timeout 2 --max-time 5 -o /dev/null -w "%{http_code}" \
      "$STREAM_SERVER/frame.jpg?source=window&app=virtualOS&max=400" 2>/dev/null || echo "0")
    [[ "$http_code" == "200" ]]
  else
    # No stream server — check if VM process is using CPU (not frozen)
    local vm_pid
    vm_pid=$(pgrep -x virtualOS 2>/dev/null | head -1)
    if [[ -n "$vm_pid" ]]; then
      local cpu
      cpu=$(ps -p "$vm_pid" -o %cpu= 2>/dev/null | awk '{print int($1)}')
      (( cpu > 0 ))
    else
      return 1
    fi
  fi
}

disk_free_gb() {
  df -g / 2>/dev/null | tail -1 | awk '{print $4}'
}

reset_hourly_counters() {
  local now
  now=$(date +%s)
  if (( now - hour_start >= 3600 )); then
    hour_start=$now
    vm_restarts_this_hour=0
    stream_restarts_this_hour=0
  fi
}

save_state() {
  python3 -c "
import json
state = {
    'check_interval': $CHECK_INTERVAL,
    'consecutive_ok': $CONSECUTIVE_OK,
    'consecutive_fail': $CONSECUTIVE_FAIL,
    'total_fixes': $TOTAL_FIXES,
    'total_checks': $TOTAL_CHECKS,
    'vm_restarts_this_hour': $vm_restarts_this_hour,
    'stream_restarts_this_hour': $stream_restarts_this_hour,
    'started_at': $STARTED_AT,
    'ts': $(date +%s),
}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f)
" 2>/dev/null || true
}

adapt_interval() {
  # Speed up checks when things are broken, slow down when stable
  if (( CONSECUTIVE_FAIL > 0 )); then
    # Problems — check more frequently
    CHECK_INTERVAL=$(( CHECK_INTERVAL > MIN_INTERVAL ? CHECK_INTERVAL - 2 : MIN_INTERVAL ))
  elif (( CONSECUTIVE_OK > 10 )); then
    # Stable for a while — relax
    CHECK_INTERVAL=$(( CHECK_INTERVAL < MAX_INTERVAL ? CHECK_INTERVAL + 5 : MAX_INTERVAL ))
  elif (( CONSECUTIVE_OK > 3 )); then
    # Recovering — slowly back off
    CHECK_INTERVAL=$(( CHECK_INTERVAL < 60 ? CHECK_INTERVAL + 1 : 60 ))
  fi
}

# ── fix actions ───────────────────────────────────────────────────────

fix_start_app() {
  if (( vm_restarts_this_hour >= MAX_VM_RESTARTS_PER_HOUR )); then
    err "Hit VM restart limit ($MAX_VM_RESTARTS_PER_HOUR/hr) — not restarting"
    return 1
  fi
  fix "Starting virtualOS app..."
  "$VOSCTL" start 2>&1 | while read -r line; do log "  $line"; done
  ((vm_restarts_this_hour++)) || true
  ((TOTAL_FIXES++)) || true
  sleep 5
}

fix_start_vm() {
  if (( vm_restarts_this_hour >= MAX_VM_RESTARTS_PER_HOUR )); then
    err "Hit VM restart limit ($MAX_VM_RESTARTS_PER_HOUR/hr) — not starting VM"
    return 1
  fi
  fix "Clicking Start button..."
  osascript -e 'tell application "System Events" to tell process "virtualOS" to click button "Start" of window "virtualOS"' 2>/dev/null
  ((vm_restarts_this_hour++)) || true
  ((TOTAL_FIXES++)) || true
  sleep 5

  # The app checks the guest login prompt and unlocks it when configured.
}

fix_start_stream() {
  if (( stream_restarts_this_hour >= MAX_STREAM_RESTARTS_PER_HOUR )); then
    err "Hit stream restart limit ($MAX_STREAM_RESTARTS_PER_HOUR/hr) — not restarting"
    return 1
  fi
  fix "Starting stream server..."
  python3 "$STREAM_SERVER_SCRIPT" --port 8899 >> "$LOG_FILE" 2>&1 &
  ((stream_restarts_this_hour++)) || true
  ((TOTAL_FIXES++)) || true
  sleep 2
}

fix_restart_vm() {
  if (( vm_restarts_this_hour >= MAX_VM_RESTARTS_PER_HOUR )); then
    err "Hit VM restart limit — not restarting"
    return 1
  fi
  warn "VM appears frozen — restarting..."
  fix "Quitting virtualOS..."
  osascript -e 'tell application "virtualOS" to quit' 2>/dev/null || true
  sleep 3
  # Force kill if still around
  if app_running; then
    fix "Force killing virtualOS..."
    pkill -9 virtualOS 2>/dev/null || true
    sleep 2
  fi
  fix_start_app
}

fix_low_disk() {
  fix "Low disk detected — clearing caches..."
  rm -rf ~/Library/Developer/Xcode/DerivedData 2>/dev/null
  rm -rf "$REPO_DIR/build" 2>/dev/null
  local freed
  freed=$(disk_free_gb)
  ok "Disk now: ${freed}G free"
  ((TOTAL_FIXES++)) || true
}

# ── main check ────────────────────────────────────────────────────────

run_check() {
  ((TOTAL_CHECKS++)) || true
  local problems=0

  # 1. Is the app running?
  if ! app_running; then
    warn "virtualOS app not running"
    fix_start_app || true
    ((problems++)) || true
  fi

  # 2. Is the VM started?
  if app_running && ! vm_running; then
    warn "App running but no VM window"
    fix_start_vm || true
    ((problems++)) || true
  fi

  # 3. Is the VM responsive (not frozen)?
  if vm_running; then
    if ! vm_responsive; then
      warn "VM appears unresponsive"
      # Wait and recheck before drastic action
      sleep 5
      if ! vm_responsive; then
        fix_restart_vm || true
        ((problems++)) || true
      fi
    fi
  fi

  # 4. Is the stream server running?
  if ! stream_running; then
    warn "Stream server not running"
    fix_start_stream || true
    ((problems++)) || true
  fi

  # 5. Disk space
  local free
  free=$(disk_free_gb)
  if (( free < 10 )); then
    warn "Critically low disk: ${free}G free"
    fix_low_disk || true
    ((problems++)) || true
  elif (( free < 20 )); then
    warn "Low disk: ${free}G free"
  fi

  # 6. Push heartbeat to relay if configured
  if [[ -n "${RELAY_URL:-}" ]] && [[ -n "${AUTH_TOKEN:-}" ]]; then
    local vm_state="unknown"
    if vm_running; then vm_state="running"
    elif app_running; then vm_state="idle"
    else vm_state="app_not_running"
    fi

    local payload="{\"state\":\"$vm_state\",\"watchdog\":true,\"problems\":$problems,\"totalFixes\":$TOTAL_FIXES,\"totalChecks\":$TOTAL_CHECKS,\"interval\":$CHECK_INTERVAL,\"consecutiveOk\":$CONSECUTIVE_OK,\"ts\":$(date +%s)}"
    curl -sf -X POST "$RELAY_URL/status" \
      -H "Authorization: Bearer $AUTH_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$payload" >/dev/null 2>&1 || true
  fi

  # Update streaks
  if (( problems == 0 )); then
    ((CONSECUTIVE_OK++)) || true
    CONSECUTIVE_FAIL=0
  else
    ((CONSECUTIVE_FAIL++)) || true
    CONSECUTIVE_OK=0
  fi

  adapt_interval
  save_state
  return $problems
}

# ── main loop ─────────────────────────────────────────────────────────

echo -e "${CYAN}virtualOS watchdog starting${NC}"
echo -e "${DIM}  check interval: ${CHECK_INTERVAL}s (adapts ${MIN_INTERVAL}s-${MAX_INTERVAL}s)${NC}"
echo -e "${DIM}  max VM restarts: ${MAX_VM_RESTARTS_PER_HOUR}/hr${NC}"
echo -e "${DIM}  max stream restarts: ${MAX_STREAM_RESTARTS_PER_HOUR}/hr${NC}"
echo -e "${DIM}  log: $LOG_FILE${NC}"
echo ""

trap 'echo ""; ok "Watchdog stopped. Checks: $TOTAL_CHECKS, Fixes: $TOTAL_FIXES"; exit 0' INT TERM

while true; do
  reset_hourly_counters

  if run_check; then
    if (( CONSECUTIVE_OK % 20 == 0 && CONSECUTIVE_OK > 0 )); then
      ok "System healthy (${CONSECUTIVE_OK} consecutive, interval: ${CHECK_INTERVAL}s)"
    fi
  fi

  sleep "$CHECK_INTERVAL"
done
