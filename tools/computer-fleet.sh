#!/usr/bin/env bash
# virtualOS fleet — run many virtual computers on one Mac and stream them out.
#
# Experimental. This script is the primary interface; tools/FLEET.md explains it.
#
# Usage:
#   computer-fleet.sh build                  compile the Swift supervisor
#   computer-fleet.sh up [vm-a,vm-b]         launch supervisor + multiplexer
#   computer-fleet.sh down                   stop supervisor + multiplexer
#   computer-fleet.sh status                 fleet + per-VM readiness
#   computer-fleet.sh capability             nested-virtualization support
#   computer-fleet.sh health                 is the fleet up?
#   computer-fleet.sh start <vm>             start one VM
#   computer-fleet.sh start-all              start every VM
#   computer-fleet.sh stop <vm>              stop one VM
#   computer-fleet.sh frame <vm> [out.jpg]   capture one VM frame as JPEG
#   computer-fleet.sh watch <vm> [dir] [n]   poll frames into dir (default /tmp)
#   computer-fleet.sh web                    serve the browser fleet viewer
#   computer-fleet.sh push <relay-url>       push status+frames to Cloudflare
#   computer-fleet.sh selftest               end-to-end fleet self-test
#   computer-fleet.sh linux <args...>        experimental Linux guest helper
#   computer-fleet.sh inspect <binary>       arm64/Mach-O read-only report
#   computer-fleet.sh fs <path>              filesystem/image read-only report
#   computer-fleet.sh containers             container runtime survey
#   computer-fleet.sh help
#
# Env:
#   VOS_FLEET_ROOT      VM bundle dir            (default ~/VirtualOSMachines)
#   VOS_FLEET_HTTP_PORT supervisor port          (default 9200)
#   VOS_FLEET_MUX_PORT  multiplexer port         (default 9300)
#   VOS_FLEET_BASE_PORT first per-VM ctrl port   (default 9100)
#   VOS_AUTH_TOKEN      relay auth token         (for: push)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLEET_DIR="$ROOT/tools/fleet"
SUPERVISOR_BIN="$FLEET_DIR/fleet-supervisor"
LINUX_BIN="$FLEET_DIR/linux-guest"
PY="$FLEET_DIR/fleet.py"
PUSH_PY="$FLEET_DIR/fleet_push.py"
HTTP_PORT="${VOS_FLEET_HTTP_PORT:-9200}"
MUX_PORT="${VOS_FLEET_MUX_PORT:-9300}"
BASE_PORT="${VOS_FLEET_BASE_PORT:-9100}"
VM_ROOT="${VOS_FLEET_ROOT:-$HOME/VirtualOSMachines}"
PID_DIR="${VOS_FLEET_PID_DIR:-/tmp}"
SUP_PID="$PID_DIR/vos-fleet-supervisor.pid"
MUX_PID="$PID_DIR/vos-fleet-mux.pid"
SUPERVISOR_URL="http://127.0.0.1:${HTTP_PORT}"
MUX_URL="http://127.0.0.1:${MUX_PORT}"

log()  { printf '%s\n' "$*"; }
die()  { printf 'computer-fleet: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

build() {
  need swiftc
  log "building fleet-supervisor..."
  swiftc -O -framework Virtualization -framework AppKit \
    "$FLEET_DIR/FleetSupervisor.swift" -o "$SUPERVISOR_BIN"
  sign_supervisor
  log "built: $SUPERVISOR_BIN"
}

# The supervisor must carry com.apple.security.virtualization to create VMs.
sign_supervisor() {
  local ent="$FLEET_DIR/fleet.entitlements"
  [[ -f "$ent" ]] || return 0
  if codesign --force --sign - --entitlements "$ent" "$SUPERVISOR_BIN" 2>/dev/null; then
    log "signed with virtualization entitlement"
  else
    log "warning: could not sign supervisor; VM boot will fail without the entitlement"
  fi
}

discover_names() {
  find "$VM_ROOT" -maxdepth 1 -name '*.bundle' -type d 2>/dev/null \
    | sed 's#.*/##; s#\.bundle$##' | paste -sd, -
}

up() {
  local names="${1:-}"
  [[ -n "$names" ]] || names="$(discover_names)"
  [[ -n "$names" ]] || die "no VM bundles found under $VM_ROOT"
  [[ -x "$SUPERVISOR_BIN" ]] || build
  log "starting fleet: $names"
  "$SUPERVISOR_BIN" --root "$VM_ROOT" --names "$names" \
    --http-port "$HTTP_PORT" --base-port "$BASE_PORT" \
    >"$PID_DIR/vos-fleet-supervisor.log" 2>&1 &
  echo $! >"$SUP_PID"
  sleep 1
  python3 "$PY" --supervisor "$SUPERVISOR_URL" serve --port "$MUX_PORT" \
    >"$PID_DIR/vos-fleet-mux.log" 2>&1 &
  echo $! >"$MUX_PID"
  log "supervisor -> $SUPERVISOR_URL (pid $(cat "$SUP_PID"))"
  log "mux        -> $MUX_URL (pid $(cat "$MUX_PID"))"
}

down() {
  local stopped=0
  for f in "$MUX_PID" "$SUP_PID"; do
    [[ -f "$f" ]] || continue
    kill "$(cat "$f")" 2>/dev/null && stopped=1 || true
    rm -f "$f"
  done
  [[ $stopped -eq 1 ]] && log "fleet stopped" || log "fleet was not running"
}

health() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "$SUPERVISOR_URL/fleet/health" || true)"
  [[ "$code" == "200" ]] && { log "fleet up ($SUPERVISOR_URL)"; return 0; }
  log "fleet down ($SUPERVISOR_URL)"; return 1
}

frame() {
  local vm="${1:-}" out="${2:-}"
  [[ -n "$vm" ]] || die "frame requires a vm name"
  [[ -n "$out" ]] || out="/tmp/vos-fleet-${vm}.jpg"
  local code
  code="$(curl -s -o "$out" -w '%{http_code}' "$SUPERVISOR_URL/fleet/frame/$vm")"
  if [[ "$code" != "200" ]]; then
    rm -f "$out"
    die "no frame for $vm (http $code) — is it running and ready?"
  fi
  log "wrote $out ($(wc -c <"$out" | tr -d ' ') bytes)"
  file "$out"
}

watch() {
  local vm="${1:-}" dir="${2:-/tmp}" count="${3:-5}"
  [[ -n "$vm" ]] || die "watch requires a vm name"
  mkdir -p "$dir"
  for i in $(seq 1 "$count"); do
    frame "$vm" "$dir/vos-fleet-${vm}-${i}.jpg" || true
    sleep 1
  done
}

web() {
  log "serving fleet viewer on http://127.0.0.1:8080 (point it at $MUX_URL)"
  (cd "$FLEET_DIR/web" && python3 -m http.server 8080 --bind 127.0.0.1)
}

push() {
  local relay="${1:-}"
  [[ -n "$relay" ]] || die "push requires a relay URL"
  [[ -n "${VOS_AUTH_TOKEN:-}" ]] || die "push requires VOS_AUTH_TOKEN"
  VOS_AUTH_TOKEN="$VOS_AUTH_TOKEN" python3 "$PUSH_PY" --relay "$relay" --supervisor "$SUPERVISOR_URL" \
    ${VOS_FLEET_PUSH_WATCH:+--watch} --verbose
}

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

cmd="${1:-help}"
shift || true
case "$cmd" in
  build) build ;;
  up) up "${1:-}" ;;
  down) down ;;
  status) python3 "$PY" --supervisor "$SUPERVISOR_URL" status "$@" ;;
  capability) python3 "$PY" --supervisor "$SUPERVISOR_URL" capability ;;
  health) health ;;
  start) python3 "$PY" --supervisor "$SUPERVISOR_URL" start "$@" ;;
  start-all|up-all) python3 "$PY" --supervisor "$SUPERVISOR_URL" up "$@" ;;
  stop) python3 "$PY" --supervisor "$SUPERVISOR_URL" stop "$@" ;;
  frame) frame "$@" ;;
  watch) watch "$@" ;;
  web) web ;;
  push) push "$@" ;;

  selftest) "$FLEET_DIR/self_test.sh" ;;
  linux)
    [[ -x "$LINUX_BIN" ]] || "$ROOT/tools/linux-guest.sh" build
    "$ROOT/tools/linux-guest.sh" "$@" ;;
  inspect) "$ROOT/tools/bin-inspect.sh" "$@" ;;
  fs) "$ROOT/tools/fs-inspect.sh" "$@" ;;
  containers) "$ROOT/tools/container-inspect.sh" "$@" ;;

  help|-h|--help) usage ;;
  *) usage; exit 2 ;;
esac
