#!/usr/bin/env bash
# virtualOS fleet runbook — reproduce the whole multi-VM box on a Mac.
#
# This is the executable record of what actually works, including the limits
# found the hard way. It is idempotent and safe by default: it never touches a
# bundle under ~/VirtualOSMachines except to read it with APFS copy-on-write.
#
# Usage:
#   runbook.sh doctor                 # check host readiness, entitlements, limits
#   runbook.sh capacity               # report Apple's active-VM limit
#   runbook.sh entitle [binary]       # sign a binary with virtualization entitle
#   runbook.sh build [new_binary]     # build and sign a new supervisor binary
#   runbook.sh agent install|remove   # install the GUI-session launch agent
#   runbook.sh clone <name> [src]     # APFS COW clone of a bundle (read src only)
#   runbook.sh boot <names>           # start the supervisor for named bundles
#   runbook.sh start <name>           # start one VM through the running supervisor
#   runbook.sh frame <vm> [out.jpg]   # capture one frame
#   runbook.sh desktop <vm> [timeout] # poll frames until non-blank, save it
#   runbook.sh stream <vm> [relay]    # push status + a live frame to Cloudflare
#   runbook.sh live [vm]              # bring the whole live stack up (idempotent)
#   runbook.sh status                 # print the whole-project truth scoreboard
#   runbook.sh netboot [src] [name]   # boot a macOS bundle with its disk over NBD
#   runbook.sh cloud [labs|r2|checkpoint|shared-base] # storage plane: labs, real R2 boot, real R2 checkpoint
#   runbook.sh stop                   # stop the launch agent / supervisor
#   runbook.sh clean                  # remove clones + agent (never ~/VirtualOSMachines)
#
# Hard-won facts encoded here:
#   1. A binary needs com.apple.security.virtualization. An ad-hoc signature
#      carries it ONLY when the process runs in the GUI (Aqua) launchd session.
#      Over plain SSH the entitlement is ignored -> "doesn't have the
#      com.apple.security.virtualization entitlement". Fix: run via a user
#      LaunchAgent (launchctl load), not nohup over ssh.
#   2. A macOS guest needs VZMacPlatformConfiguration + VZMacOSBootLoader.
#      The generic platform (needed for nested virt) has no macOS boot path, so
#      macOS-guests and nested-virt are mutually exclusive today.
#   3. Apple caps the number of concurrently active VMs per host. On this M4
#      Mac mini the observed limit is 2 including any already-running VM.
#      Exceeding it: "The maximum supported number of active virtual machines
#      has been reached." Check with `runbook.sh capacity`.
#   4. A guest takes ~90-120s to render its desktop; early frames are black.
#      `desktop` polls until the JPEG grows past a threshold.
#   5. Boot a CLONE, never the live bundle: the live bundle is locked by the
#      running app and its disk is sacred.
#   6. Cloudflare returns error 1010 to the default python urllib User-Agent.
#      fleet_push.py sends a normal UA; anything else must do the same.
#   7. Frames travel raw JPEG through the relay and land in R2; a push is only
#      real when the SHA-256 of the fetched frame matches the one sent.
#   8. When the active-VM cap is hit, the supervisor does NOT fail: it queues the
#      VM as "pending" and retries every 15s, so a stopped VM's slot is promoted
#      to a waiting one automatically. Boot more names than slots safely.
set -euo pipefail

FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
SRC_ROOT="${VOS_FLEET_SRC:-$HOME/VirtualOSMachines}"
MACHINES="$FLEET_HOME/machines"
AGENT_LABEL="${VOS_FLEET_AGENT_LABEL:-com.virtualos.fleet}"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
ENTITLEMENTS="$FLEET_HOME/tools/fleet/fleet.entitlements"
SUPERVISOR="${VOS_SUPERVISOR:-$FLEET_HOME/tools/fleet/fleet-supervisor}"
HTTP_PORT="${VOS_FLEET_HTTP_PORT:-9225}"
BASE_PORT="${VOS_FLEET_BASE_PORT:-9125}"

log()  { printf '%s\n' "$*"; }
die()  { printf 'runbook: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing tool: $1"; }

doctor() {
  log "== virtualOS fleet doctor =="
  log "host      : $(sw_vers -productName) $(sw_vers -productVersion) ($(sw_vers -buildVersion))"
  log "cpu/mem   : $(sysctl -n hw.ncpu) cpus / $(( $(sysctl -n hw.memsize) / 1073741824 )) GiB"
  log "hv_support: $(sysctl -n kern.hv_support 2>/dev/null || echo unknown)"
  log "fleet home: $FLEET_HOME"
  [[ -d "$FLEET_HOME/tools/fleet" ]] && log "fleet code: present" || log "fleet code: MISSING (sync tools/fleet/ here)"
  [[ -x "$SUPERVISOR" ]] && log "supervisor: built" || log "supervisor: not built"
  if [[ -x "$SUPERVISOR" ]]; then
    local ent
    ent="$(codesign -d --entitlements :- "$SUPERVISOR" 2>/dev/null | grep -c virtualization || true)"
    [[ "$ent" -gt 0 ]] && log "entitlement: virtualization present" || log "entitlement: MISSING -> run: runbook.sh entitle"
  fi
  [[ -f "$AGENT_PLIST" ]] && log "launch agent: installed" || log "launch agent: not installed"
  log "src bundles : $(find "$SRC_ROOT" -maxdepth 1 -name '*.bundle' -type d 2>/dev/null | wc -l | tr -d ' ')"
  capacity
}

capacity() {
  log "== capacity =="
  local xpc running
  xpc="$(pgrep -f 'com.apple.Virtualization.VirtualMachine' | wc -l | tr -d ' ')"
  # The honest VM count is the supervisor's, not the raw process count: each VM
  # spawns one or more Virtualization XPC services, and stale ones linger.
  running="$(curl -s -m 3 "http://127.0.0.1:$HTTP_PORT/fleet/status" 2>/dev/null \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(sum(1 for i in d.get("instances",[]) if i.get("state")=="running"))
except Exception:
    print("")' 2>/dev/null)"
  if [[ -n "$running" ]]; then
    log "VMs running (supervisor): $running"
  else
    log "VMs running (supervisor): unknown (supervisor not responding)"
  fi
  log "Virtualization XPC services seen: $xpc"
  if [[ -n "$running" ]] && (( xpc > running )); then
    log "WARN: $((xpc - running)) XPC service(s) beyond the $running running VM(s);"
    log "      these may be orphaned and still hold the host's VM cap. Inspect with:"
    log "      pgrep -fl com.apple.Virtualization.VirtualMachine"
  fi
  log "note: Apple caps concurrent active VMs per host. On this host the observed"
  log "      limit is 2 (including any already-running VM). Boot a clone only when"
  log "      the count is below the limit."
}

entitle() {
  local bin="${1:-$SUPERVISOR}"
  local entitlements="${2:-$ENTITLEMENTS}"
  [[ -f "$bin" ]] || die "no such binary: $bin"
  [[ -f "$entitlements" ]] || die "missing entitlements: $entitlements"
  codesign --force --sign - --entitlements "$entitlements" "$bin"
  log "signed: $bin"
  codesign -d --entitlements :- "$bin" 2>/dev/null | grep -q virtualization \
    && log "verified: virtualization entitlement" || die "entitlement not applied"
}

build() (
  local output="${1:-$SUPERVISOR}" scratch
  [[ ! -e "$output" ]] || die "build target exists; choose a new binary path"
  mkdir -p "$(dirname "$output")"
  scratch="$(mktemp "$output.build.XXXXXX")"
  trap 'rm -f "$scratch"' EXIT
  swiftc -O -framework Virtualization -framework AppKit "$(dirname "$0")/FleetSupervisor.swift" -o "$scratch"
  entitle "$scratch" "$(dirname "$0")/fleet.entitlements"
  mv "$scratch" "$output"
  log "built signed supervisor: $output"
)

agent_install() {
  local names="$1"
  mkdir -p "$(dirname "$AGENT_PLIST")"
  cat > "$AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$AGENT_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$SUPERVISOR</string>
    <string>--root</string><string>$MACHINES</string>
    <string>--names</string><string>$names</string>
    <string>--http-port</string><string>$HTTP_PORT</string>
    <string>--base-port</string><string>$BASE_PORT</string>
    <string>--start</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$FLEET_HOME/sup-agent.log</string>
  <key>StandardErrorPath</key><string>$FLEET_HOME/sup-agent.log</string>
</dict>
</plist>
PLIST
  launchctl unload "$AGENT_PLIST" 2>/dev/null || true
  sleep 1
  launchctl load "$AGENT_PLIST"
  log "agent loaded: $AGENT_LABEL (names=$names)"
}

agent_remove() {
  launchctl unload "$AGENT_PLIST" 2>/dev/null || true
  rm -f "$AGENT_PLIST"
  log "agent removed"
}

clone() {
  local name="${1:-}" src="${2:-}"
  [[ -n "$name" ]] || die "clone requires a name"
  if [[ -z "$src" ]]; then
    src="$(find "$SRC_ROOT" -maxdepth 1 -name '*.bundle' -type d 2>/dev/null | head -1)"
  fi
  [[ -d "$src" ]] || die "no source bundle (looked in $SRC_ROOT)"
  local dst="$FLEET_HOME/vm-$name.bundle"
  rm -rf "$dst"; mkdir -p "$dst" "$MACHINES"
  for f in Disk.img AuxiliaryStorage HardwareModel MachineIdentifier Parameters.txt; do
    [[ -e "$src/$f" ]] || continue
    cp -c "$src/$f" "$dst/$f"    # APFS copy-on-write: instant, shares extents
  done
  ln -sfn "$dst" "$MACHINES/$name.bundle"
  log "cloned $src -> $dst (COW; source untouched)"
  log "linked $MACHINES/$name.bundle"
}

boot() {
  local names="${1:-}"
  [[ -n "$names" ]] || die "boot requires names (comma-separated)"
  [[ -x "$SUPERVISOR" ]] || die "supervisor not built"
  agent_install "$names"
  sleep 3
  curl -s "http://127.0.0.1:$HTTP_PORT/fleet/status" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("fleet count:", d.get("count"))
for i in d.get("instances", []):
    name=i.get("name","?"); state=i.get("state","?"); ready=i.get("ready"); err=i.get("error","")
    print("  %-8s %-8s ready=%s %s" % (name, state, ready, err))
'
}

frame() {
  local vm="${1:-}" out="${2:-/tmp/vos-$1.jpg}"
  [[ -n "$vm" ]] || die "frame requires a vm"
  local code
  code="$(curl -s -o "$out" -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT/fleet/frame/$vm")"
  [[ "$code" == "200" ]] || { rm -f "$out"; die "no frame for $vm (http $code)"; }
  log "wrote $out ($(wc -c <"$out" | tr -d ' ') bytes)"
}

desktop() {
  local vm="${1:-}" timeout="${2:-180}" out="${3:-/tmp/vos-$1-desktop.jpg}"
  [[ -n "$vm" ]] || die "desktop requires a vm"
  local waited=0 threshold="${VOS_DESKTOP_MIN_BYTES:-100000}"
  [[ "$threshold" =~ ^[0-9]+$ ]] && (( threshold >= 10000 && threshold <= 10000000 )) || die "invalid VOS_DESKTOP_MIN_BYTES"
  while [[ $waited -lt $timeout ]]; do
    local code bytes
    code="$(curl -s -o "$out.tmp" -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT/fleet/frame/$vm" || true)"
    if [[ "$code" == "200" ]]; then
      bytes="$(wc -c <"$out.tmp" | tr -d ' ')"
      if [[ "$bytes" -gt "$threshold" ]]; then
        mv "$out.tmp" "$out"
        log "desktop ready after ${waited}s: $out ($bytes bytes)"
        return 0
      fi
    fi
    rm -f "$out.tmp"
    sleep 10; waited=$((waited + 10))
    log "waiting for $vm desktop... ${waited}s (last ${bytes:-0} bytes)"
  done
  die "no desktop frame for $vm within ${timeout}s"
}

# netboot: boot a macOS bundle whose disk is served over loopback NBD. The
# source bundle is read-only; all guest writes land in a bounded overlay. This
# proves a machine can run with no local disk at all.
netboot() {
  local src="${1:-vm-clone}" name="${2:-vm-net}" tmo="${3:-220}"
  local http_port="${VOS_NETBOOT_HTTP_PORT:-9240}"
  local cs
  cs="$(cd "$(dirname "$0")/.." && pwd)/computer-space.sh"
  [[ -f "$cs" ]] || die "computer-space.sh not found"
  local srcdisk="$FLEET_HOME/$src.bundle/Disk.img"
  [[ -f "$srcdisk" ]] || die "no source disk: $srcdisk"
  [[ "$FLEET_HOME" != "$SRC_ROOT" ]] || die "refusing to serve the production store"
  local overlay_dir="/private/tmp/vos-nbd-bundle-$name"
  rm -rf "$overlay_dir"; mkdir -p "$overlay_dir"

  # Choose a free loopback port so a stale server can never wedge the run.
  local nbd_port="${VOS_NETBOOT_NBD_PORT:-0}" p
  if [[ "$nbd_port" == 0 ]]; then
    for p in $(seq 40900 40999); do
      lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1 || { nbd_port="$p"; break; }
    done
  fi
  [[ "$nbd_port" != 0 ]] || die "no free NBD port in 40900-40999"

  VOS_NBD_BUNDLE_ROOT="$FLEET_HOME" VOS_NBD_BUNDLE_NAME="$src" \
    VOS_NBD_BUNDLE_OVERLAY="$overlay_dir/overlay.sqlite" VOS_NBD_BUNDLE_PORT="$nbd_port" \
    bash "$cs" --nbd-bundle-serve > "$overlay_dir/server.log" 2>&1 &
  local srv=$!
  local i=0; while [[ $i -lt 60 ]]; do grep -q '"ready"' "$overlay_dir/server.log" 2>/dev/null && break; sleep 0.2; i=$((i+1)); done
  grep -q '"ready"' "$overlay_dir/server.log" || { kill "$srv" 2>/dev/null; die "NBD server did not become ready"; }
  log "NBD serving $src on 127.0.0.1:$nbd_port"

  local dst="$FLEET_HOME/$name.bundle"
  rm -rf "$dst"; mkdir -p "$dst"
  local f
  for f in HardwareModel MachineIdentifier AuxiliaryStorage; do
    cp -c "$FLEET_HOME/$src.bundle/$f" "$dst/$f" 2>/dev/null || cp "$FLEET_HOME/$src.bundle/$f" "$dst/$f"
  done
  python3 - "$dst/Parameters.txt" "$nbd_port" <<'PY'
import json, sys
open(sys.argv[1], "w").write(json.dumps({
    "cpuCount": 3, "memorySizeInGB": 4,
    "screenWidth": 1500, "screenHeight": 900,
    "nbdURL": "nbd://127.0.0.1:%s/virtualos-test" % sys.argv[2]}, indent=2))
PY
  mkdir -p "$MACHINES"; ln -sfn "$dst" "$MACHINES/$name.bundle"
  log "prepared disk-less bundle $dst (no Disk.img)"

  local label="com.virtualos.netboot.$name"
  local plist="$HOME/Library/LaunchAgents/$label.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>$SUPERVISOR</string>
    <string>--root</string><string>$MACHINES</string>
    <string>--names</string><string>$name</string>
    <string>--http-port</string><string>$http_port</string>
    <string>--base-port</string><string>9140</string>
    <string>--start</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$overlay_dir/boot.log</string>
  <key>StandardErrorPath</key><string>$overlay_dir/boot.log</string>
</dict></plist>
PLIST
  launchctl unload "$plist" 2>/dev/null || true; sleep 1
  launchctl load "$plist"
  log "booting $name from NBD (see $overlay_dir/boot.log)"

  local out="/tmp/vos-$name-desktop.jpg" waited=0 bytes=0
  while [[ $waited -lt $tmo ]]; do
    local code
    code="$(curl -s -o "$out.tmp" -w '%{http_code}' "http://127.0.0.1:$http_port/fleet/frame/$name" || true)"
    if [[ "$code" == "200" ]]; then
      bytes="$(wc -c <"$out.tmp" | tr -d ' ')"
      if [[ "$bytes" -gt 100000 ]]; then mv "$out.tmp" "$out"; log "desktop ready after ${waited}s: $out ($bytes bytes)"; break; fi
    fi
    rm -f "$out.tmp"; sleep 10; waited=$((waited + 10))
  done
  [[ -f "$out" ]] || log "no desktop yet; VM may still be booting"

  launchctl unload "$plist" 2>/dev/null || true; rm -f "$plist"
  # The server runs as a child python; kill by its command line so no listener
  # is left holding the port after teardown.
  kill "$srv" 2>/dev/null || true
  pkill -f "cloud_disk.py bundle-serve $FLEET_HOME $src " 2>/dev/null || true
  pkill -f "cloud_disk.py bundle-serve.*vos-nbd-bundle-$name" 2>/dev/null || true
  sleep 2
  python3 - "$overlay_dir/server.log" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip().startswith('{')]
conn = [r['bundle_nbd_connection'] for r in rows if 'bundle_nbd_connection' in r]
if conn:
    m = conn[-1]['metrics']
    print("netboot=NBD_verified reads=%s read_bytes=%s writes=%s write_bytes=%s errors=%s" % (
        conn[-1]['reads'], m['read']['request_bytes_total'], conn[-1]['writes'],
        m['write']['request_bytes_total'], m['read']['errors'] + m['write']['errors']))
else:
    print("netboot=no_session_summary_yet (server ended before a full session closed)")
PY
  log "netboot teardown: source RAW never modified; guest writes were overlay-only"
}

stream() {
  local relay="${RELAY_URL:-${1:-https://virtualos-relay.ai-c24.workers.dev}}"
  local token="${AUTH_TOKEN:-${VOS_AUTH_TOKEN:-}}"
  local env_file="$HOME/.config/virtualos/relay.env"
  if [[ -r "$env_file" ]]; then
    python3 "$FLEET_HOME/tools/fleet/fleet_push.py" \
      --relay "$relay" --supervisor "http://127.0.0.1:$HTTP_PORT" \
      --token-file "$env_file" --verbose
    return
  fi
  [[ -n "$token" ]] || die "no token: set AUTH_TOKEN or write $env_file"
  VOS_AUTH_TOKEN="$token" python3 "$FLEET_HOME/tools/fleet/fleet_push.py" \
    --relay "$relay" --supervisor "http://127.0.0.1:$HTTP_PORT" --verbose
}

stop() { agent_remove; pkill -f "$SUPERVISOR" 2>/dev/null || true; log "fleet stopped"; }

# The storage plane lives in computer-space.sh. This fronts its disposable labs,
# and, when credentials + a config are present, the real Cloudflare R2 boot.
cloud() {
  local cs="$FLEET_HOME/tools/computer-space.sh"
  [[ -x "$cs" ]] || cs="$(cd "$(dirname "$0")/.." && pwd)/computer-space.sh"
  [[ -f "$cs" ]] || die "computer-space.sh not found"
  case "${1:-labs}" in
    labs)
      bash "$cs" --cloud-disk-self-test
      bash "$cs" --cloud-disk-checkpoint-self-test
      for backend in objects raw r2-fake; do
        VOS_NBD_VZ_BACKEND="$backend" bash "$cs" --nbd-vz-lab | tail -1
      done
      bash "$cs" --raw-hole-lab | tail -1
      ;;
    r2)
      local cfg="${VOS_CLOUD_DISK_CONFIG:-$HOME/.config/virtualos/r2-lab-config.json}"
      local env_file="$HOME/.config/virtualos/r2.env"
      [[ -r "$env_file" ]] && source "$env_file"
      [[ -r "$cfg" ]] || die "no config: set VOS_CLOUD_DISK_CONFIG or write $cfg"
      VOS_CLOUD_DISK_CONFIG="$cfg" bash "$cs" --cloud-disk-r2-boot-lab
      ;;
    checkpoint)
      local cfg="${VOS_CLOUD_DISK_CONFIG:-$HOME/.config/virtualos/r2-chk-config.json}"
      local env_file="$HOME/.config/virtualos/r2.env"
      [[ -r "$env_file" ]] && source "$env_file"
      [[ -r "$cfg" ]] || die "no config: set VOS_CLOUD_DISK_CONFIG or write $cfg"
      VOS_CLOUD_DISK_CONFIG="$cfg" bash "$cs" --cloud-disk-r2-checkpoint-lab
      ;;
    shared-base)
      # Compact fleet: prove base+delta round-trips and serve it over NBD.
      local exp="$(cd "$(dirname "$cs")/experiments" 2>/dev/null && pwd)"
      [[ -d "$exp" ]] || die "experiments/ not found next to computer-space.sh"
      python3 "$exp/shared_base.py" selftest
      python3 "$exp/base_delta_serve_test.py"
      bash "$(dirname "$cs")/fleet/go-live.sh" --self-test
      ;;
    *) die "cloud [labs|r2|checkpoint|shared-base]";;
  esac
}
clean() {
  agent_remove
  pkill -f "$SUPERVISOR" 2>/dev/null || true
  rm -rf "$MACHINES" "$FLEET_HOME"/vm-*.bundle
  log "removed clones under $FLEET_HOME (source $SRC_ROOT untouched)"
}

case "${1:-doctor}" in
  doctor) doctor ;;
  capacity) capacity ;;
  entitle) shift; entitle "${1:-}" ;;
  build) shift; build "${1:-}" ;;
  agent) shift; case "${1:-}" in install) shift; agent_install "${1:-vm-a}";; remove) agent_remove;; *) die "agent install|remove";; esac ;;
  clone) shift; clone "${1:-}" "${2:-}" ;;
  boot) shift; boot "${1:-}" ;;
  start) shift; python3 "$(dirname "$0")/fleet.py" --supervisor "http://127.0.0.1:${VOS_SUP_PORT:-$HTTP_PORT}" start "${1:?start requires a machine name}" ;;
  frame) shift; frame "${1:-}" "${2:-}" ;;
  desktop) shift; desktop "${1:-}" "${2:-}" "${3:-}" ;;
  stream) shift; stream "${1:-}" ;;
  netboot) shift; netboot "${1:-}" "${2:-}" "${3:-}" ;;
  cloud) shift; cloud "${1:-labs}" ;;
  live) shift; exec "$(cd "$(dirname "$0")" && pwd)/go-live.sh" "$@" ;;
  status) exec "$(cd "$(dirname "$0")" && pwd)/plan-status.sh" "${2:-}" ;;
  stop) stop ;;
  clean) clean ;;
  *) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
