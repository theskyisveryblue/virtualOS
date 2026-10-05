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
#   runbook.sh agent install|remove   # install the GUI-session launch agent
#   runbook.sh clone <name> [src]     # APFS COW clone of a bundle (read src only)
#   runbook.sh boot <names>           # start the supervisor for named bundles
#   runbook.sh frame <vm> [out.jpg]   # capture one frame
#   runbook.sh desktop <vm> [timeout] # poll frames until non-blank, save it
#   runbook.sh stream <vm> [relay]    # push status + a live frame to Cloudflare
#   runbook.sh cloud [labs|r2|checkpoint] # storage plane: labs, real R2 boot, real R2 checkpoint
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
AGENT_LABEL="com.virtualos.fleet"
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
ENTITLEMENTS="$FLEET_HOME/tools/fleet/fleet.entitlements"
SUPERVISOR="$FLEET_HOME/tools/fleet/fleet-supervisor"
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
  local active
  active="$(pgrep -f 'com.apple.Virtualization.VirtualMachine' | wc -l | tr -d ' ')"
  log "active VM XPC processes: $active"
  log "note: Apple caps concurrent active VMs per host. On this host the observed"
  log "      limit is 2 (including any already-running VM). Boot a clone only when"
  log "      the count is below the limit."
}

entitle() {
  local bin="${1:-$SUPERVISOR}"
  [[ -f "$bin" ]] || die "no such binary: $bin"
  [[ -f "$ENTITLEMENTS" ]] || die "missing entitlements: $ENTITLEMENTS"
  codesign --force --sign - --entitlements "$ENTITLEMENTS" "$bin"
  log "signed: $bin"
  codesign -d --entitlements :- "$bin" 2>/dev/null | grep -q virtualization \
    && log "verified: virtualization entitlement" || die "entitlement not applied"
}

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
  local waited=0 threshold=100000
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

stream() {
  local relay="${RELAY_URL:-}" token="${AUTH_TOKEN:-${VOS_AUTH_TOKEN:-}}"
  local env_file="$HOME/.config/virtualos/relay.env"
  if [[ -r "$env_file" ]]; then
    # shellcheck disable=SC1090
    source "$env_file"
    relay="${relay:-${RELAY_URL:-}}"; token="${token:-${AUTH_TOKEN:-}}"
  fi
  [[ -n "$relay" ]] || relay="${1:-}"
  [[ -n "$relay" ]] || die "no relay: set RELAY_URL, or pass relay URL, or write $env_file"
  [[ -n "$token" ]] || die "no token: set AUTH_TOKEN or write $env_file"
  python3 "$FLEET_HOME/tools/fleet/fleet_push.py" \
    --relay "$relay" --supervisor "http://127.0.0.1:$HTTP_PORT" --token "$token" --verbose
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
    *) die "cloud [labs|r2|checkpoint]";;
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
  agent) shift; case "${1:-}" in install) shift; agent_install "${1:-vm-a}";; remove) agent_remove;; *) die "agent install|remove";; esac ;;
  clone) shift; clone "${1:-}" "${2:-}" ;;
  boot) shift; boot "${1:-}" ;;
  frame) shift; frame "${1:-}" "${2:-}" ;;
  desktop) shift; desktop "${1:-}" "${2:-}" "${3:-}" ;;
  stream) shift; stream "${1:-}" ;;
  cloud) shift; cloud "${1:-labs}" ;;
  stop) stop ;;
  clean) clean ;;
  *) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
