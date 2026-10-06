#!/usr/bin/env bash
# Bring the whole live stack up in one shot — idempotent, safe to re-run.
#
#   tools/fleet/go-live.sh [VM_NAME]
#
# Ensures, in order:
#   1. a fleet VM bundle exists        (COW clone of production if missing)
#   2. the fleet supervisor is running  (LaunchAgent, GUI/Aqua session)
#   3. at least one VM is running       (queues if the host cap is reached)
#   4. the relay stream loop is running (LaunchAgent, KeepAlive)
#   5. one heartbeat + frame reaches the relay, then reports liveness
#
# Run it whenever the stack looks stale; it converges to "live".
set -euo pipefail

NAME="${1:-${VOS_FLEET_VM:-vm-a}}"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
PROD_BUNDLE="${VOS_PROD_BUNDLE:-$HOME/VirtualOSMachines/virtualOS.bundle}"
SUP_PORT="${VOS_SUP_PORT:-9225}"
RELAY_ENV="${VOS_RELAY_ENV:-$HOME/.config/virtualos/relay.env}"
FLEET_TOOLS="${VOS_FLEET_TOOLS:-$FLEET_HOME/tools}"

say() { printf '== %s\n' "$*"; }
fleet_ok() { curl -s -m 4 "http://127.0.0.1:$SUP_PORT/fleet/status" >/dev/null 2>&1; }

say "1. ensure a fleet machine bundle: $NAME"
BUNDLE="$FLEET_HOME/$NAME.bundle"
mkdir -p "$FLEET_HOME/machines"
if [[ -f "$BUNDLE/Disk.img" ]]; then
  echo "   bundle present"
elif [[ -f "$PROD_BUNDLE/Disk.img" ]]; then
  echo "   cloning production (COW, ~0 extra space)"
  rm -rf "$BUNDLE"
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

say "2. fleet supervisor"
if fleet_ok; then
  echo "   already up on :$SUP_PORT"
else
  echo "   starting (LaunchAgent)"
  bash "$FLEET_TOOLS/fleet/runbook.sh" boot "$NAME" 2>&1 | tail -3
  sleep 4
fi

say "3. a VM is running"
if curl -s -m 4 "http://127.0.0.1:$SUP_PORT/fleet/status" | grep -q '"state":"running"'; then
  echo "   running"
else
  bash "$FLEET_TOOLS/fleet/runbook.sh" start "$NAME" 2>&1 | tail -2 || true
fi

say "4. relay stream loop"
launchctl kickstart -k "gui/$(id -u)/com.virtualos.stream" 2>/dev/null \
  || launchctl load "$HOME/Library/LaunchAgents/com.virtualos.stream.plist" 2>/dev/null \
  || echo "   (no stream LaunchAgent; run fleet_push.py manually)"

say "5. push once and verify liveness"
if [[ -f "$RELAY_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$RELAY_ENV"
  python3 "$FLEET_TOOLS/fleet/fleet_push.py" --relay "$RELAY_URL" \
    --supervisor "http://127.0.0.1:$SUP_PORT" --token "$AUTH_TOKEN" --verbose 2>&1 | tail -3
  curl -s -m 8 -H "Authorization: Bearer $AUTH_TOKEN" "$RELAY_URL/fleet/status" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("   relay live:", not d["liveness"]["isStale"], "hb_age:", d["liveness"]["lastHeartbeatAgeSec"], "s")'
else
  echo "   no $RELAY_ENV; skipping relay push" >&2
fi
say "done"
