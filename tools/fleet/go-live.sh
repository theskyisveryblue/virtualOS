#!/usr/bin/env bash
# Bring the whole live stack up in one shot — idempotent, safe to re-run.
#
#   tools/fleet/go-live.sh [VM_NAME] [--prepare-only|--boot-only]
#   tools/fleet/go-live.sh --self-test
#
# Ensures, in order:
#   1. a fleet VM bundle exists        (shared disk for new machines)
#   2. the fleet supervisor is running  (LaunchAgent, GUI/Aqua session)
#   3. the requested VM is running      (queues if the host cap is reached)
#   4. the relay stream loop is running (LaunchAgent, KeepAlive)
#   5. one heartbeat + frame reaches the relay, then reports liveness
#
# Run it whenever the stack looks stale; it converges to "live".
set -euo pipefail

if [[ "${1:-}" == "--self-test" ]]; then
  exec python3 "$(cd "$(dirname "$0")" && pwd)/test_go_live.py"
fi
PREPARE_ONLY=false
BOOT_ONLY=false
if [[ "${1:-}" == "--prepare-only" ]]; then
  PREPARE_ONLY=true
  shift
fi
NAME="${1:-${VOS_FLEET_VM:-vm-a}}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ "${1:-}" == "--prepare-only" ]]; then PREPARE_ONLY=true; shift; fi
if [[ "${1:-}" == "--boot-only" ]]; then BOOT_ONLY=true; shift; fi
if [[ $# -gt 0 || ! "$NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
  echo "usage: go-live.sh [VM_NAME] [--prepare-only|--boot-only]" >&2
  exit 1
fi
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
PROD_BUNDLE="${VOS_PROD_BUNDLE:-$HOME/VirtualOSMachines/virtualOS.bundle}"
SUP_PORT="${VOS_SUP_PORT:-9225}"
RELAY_ENV="${VOS_RELAY_ENV:-$HOME/.config/virtualos/relay.env}"
FLEET_TOOLS="${VOS_FLEET_TOOLS:-$FLEET_HOME/tools}"

say() { printf '== %s\n' "$*"; }
fleet_ok() { curl -fsS -m 4 "http://127.0.0.1:$SUP_PORT/fleet/status" >/dev/null 2>&1; }

say "1. ensure a fleet machine bundle: $NAME"
BUNDLE="$FLEET_HOME/$NAME.bundle"
if [[ -f "$FLEET_HOME/cloud-machines/$NAME.json" &&
      ! -f "${VOS_SHARED_DISK_CONFIG:-$FLEET_HOME/shared-disks/$NAME.json}" ]]; then
  echo "   $NAME is stored cold in R2; restore its base and pinned delta before go-live" >&2
  exit 1
fi
mkdir -p "$FLEET_HOME/machines"
SHARED_DISK_TOOL="$(cd "$(dirname "$0")" && pwd)/shared_disk.py"
python3 "$SHARED_DISK_TOOL" prepare "$FLEET_HOME" "$NAME"
if [[ -e "$BUNDLE" || -L "$BUNDLE" ]]; then
  # An existing bundle may use NBD and deliberately have no Disk.img. Never
  # replace it: its platform files and parameters belong to this machine.
  python3 - "$BUNDLE" <<'PY'
import json, pathlib, sys
from urllib.parse import urlsplit
bundle = pathlib.Path(sys.argv[1])
try:
    if not bundle.is_dir():
        raise ValueError("bundle is not a directory")
    params = bundle / "Parameters.txt"
    cfg = json.loads(params.read_text()) if params.exists() else {}
    if not isinstance(cfg, dict):
        raise ValueError("Parameters.txt must contain a JSON object")
    nbd = cfg.get("nbdURL")
    if nbd is not None:
        if not isinstance(nbd, str):
            raise ValueError("nbdURL must be a string")
        url = urlsplit(nbd)
        if url.scheme != "nbd" or not url.hostname or not url.port:
            raise ValueError("nbdURL must include an nbd host and port")
        print("   existing NBD bundle preserved")
    elif not (bundle / "Disk.img").is_file():
        raise ValueError("bundle has neither Disk.img nor nbdURL")
    else:
        print("   existing local-disk bundle preserved")
    for name in ("HardwareModel", "MachineIdentifier"):
        if not (bundle / name).is_file():
            raise ValueError("missing " + name)
except (OSError, ValueError) as exc:
    sys.exit("   cannot prepare existing bundle: " + str(exc))
PY
elif [[ -f "$PROD_BUNDLE/Disk.img" ]]; then
  if [[ "${VOS_ALLOW_LOCAL_CLONE:-}" != 1 ]]; then
    echo "   new machines require shared storage: configure shared-disks/$NAME.json first" >&2
    echo "   VOS_ALLOW_LOCAL_CLONE=1 explicitly permits a full local COW clone" >&2
    exit 1
  fi
  echo "   cloning production (explicit local COW clone; storage grows as the disks diverge)"
  cp -c -R "$PROD_BUNDLE" "$BUNDLE"
  # a clone boots from its own disk; drop any nbdURL left over from a lab bundle
  python3 - "$BUNDLE/Parameters.txt" <<'PY'
import json, sys
p = sys.argv[1]
try:
    cfg = json.load(open(p))
except Exception:
    cfg = {}
cfg.pop("nbdURL", None)
cfg.setdefault("cpuCount", 4)
cfg.setdefault("memorySizeInGB", 4)
cfg.setdefault("screenWidth", 1500)
cfg.setdefault("screenHeight", 900)
json.dump(cfg, open(p, "w"), indent=2)
PY
else
  echo "   no production bundle at $PROD_BUNDLE and no $BUNDLE" >&2
  exit 1
fi
ln -sfn "$BUNDLE" "$FLEET_HOME/machines/$NAME.bundle"
if [[ "$PREPARE_ONLY" == true ]]; then
  say "prepared $NAME"
  exit 0
fi

say "1b. shared disk service"
python3 "$SHARED_DISK_TOOL" start "$FLEET_HOME" "$NAME"

say "2. fleet supervisor"
if fleet_ok; then
  echo "   already up on :$SUP_PORT"
else
  echo "   starting (LaunchAgent)"
  bash "$FLEET_TOOLS/fleet/runbook.sh" boot "$NAME" 2>&1 | tail -3
  sleep 4
fi

say "3. requested VM: $NAME"
if curl -fsS -m 4 "http://127.0.0.1:$SUP_PORT/fleet/status" | python3 -c '
import json, sys
status = json.load(sys.stdin)
sys.exit(0 if any(m.get("name") == sys.argv[1] and m.get("state") in
                 ("running", "starting", "pending")
                 for m in status.get("instances", [])) else 1)
' "$NAME"; then
  echo "   running or already queued"
else
  bash "$FLEET_TOOLS/fleet/runbook.sh" start "$NAME" 2>&1 | tail -2
fi

if [[ "$BOOT_ONLY" == true ]]; then
  say "boot requested for $NAME"
  exit 0
fi

say "4. relay stream loop"
launchctl kickstart -k "gui/$(id -u)/com.virtualos.stream" 2>/dev/null \
  || launchctl load "$HOME/Library/LaunchAgents/com.virtualos.stream.plist" 2>/dev/null \
  || echo "   (no stream LaunchAgent; run fleet_push.py manually)"

say "5. push once and verify liveness"
if [[ -f "$RELAY_ENV" ]]; then
  RELAY_URL="${RELAY_URL:-https://virtualos-relay.ai-c24.workers.dev}"
  python3 "$FLEET_TOOLS/fleet/fleet_push.py" --relay "$RELAY_URL" \
    --supervisor "http://127.0.0.1:$SUP_PORT" --token-file "$RELAY_ENV" --verbose 2>&1 | tail -3
  python3 "$FLEET_TOOLS/fleet/relay_read.py" --token-file "$RELAY_ENV" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("   relay live:", not d["liveness"]["isStale"], "hb_age:", d["liveness"]["lastHeartbeatAgeSec"], "s")'
else
  echo "   no $RELAY_ENV; skipping relay push" >&2
fi
say "done"
