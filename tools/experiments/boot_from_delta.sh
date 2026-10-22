#!/usr/bin/env bash
# Verify a real guest through the managed shared-disk boot path. Sources must
# be stopped-disk snapshots in this isolated fleet, never live disks.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-shared-fleet}"
FLEET_TOOLS="${VOS_FLEET_TOOLS:-$FLEET_HOME/tools}"
BASE_BUNDLE="${VOS_EXP_BASE_BUNDLE:-$FLEET_HOME/vm-base.bundle}"
MACHINE_BUNDLE="${VOS_EXP_MACHINE_BUNDLE:-$FLEET_HOME/vm-machine.bundle}"
NAME="${VOS_EXP_NAME:-vm-shared}"
OUT="${VOS_EXP_OUT:-$FLEET_HOME/deltas/$NAME}"
HTTP_PORT="${VOS_EXP_HTTP_PORT:-9254}"
NBD_PORT="${VOS_EXP_NBD_PORT:-40994}"
TMO="${VOS_EXP_TMO:-300}"
RELEASE_VM="${VOS_EXP_RELEASE_VM:-}"
OLD_PORT="${VOS_EXP_EXISTING_SUP_PORT:-9225}"
WATCHDOG="${VOS_EXP_WATCHDOG_LABEL:-com.virtualos.fleet-watchdog}"
LABEL="com.virtualos.exp.$NAME"
DOMAIN="gui/$(id -u)"
RELEASED=0
WATCHDOG_PAUSED=0
STARTED=0
[[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || { echo invalid_machine_name >&2; exit 1; }
[[ -f "$BASE_BUNDLE/Disk.img" && -f "$MACHINE_BUNDLE/Disk.img" ]] || { echo missing_snapshots >&2; exit 1; }
[[ -x "$FLEET_TOOLS/fleet/fleet-supervisor" ]] || { echo missing_supervisor >&2; exit 1; }

cleanup() {
  local result=$?
  if (( STARTED )); then
    python3 "$FLEET_TOOLS/fleet/fleet.py" --supervisor "http://127.0.0.1:$HTTP_PORT" stop "$NAME" >/dev/null 2>&1 || true
    sleep 2
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootout "$DOMAIN/com.virtualos.disk.$NAME" 2>/dev/null || true
  fi
  if (( RELEASED )); then
    python3 "$FLEET_TOOLS/fleet/fleet.py" --supervisor "http://127.0.0.1:$OLD_PORT" start "$RELEASE_VM" || result=1
    for _ in $(seq 1 30); do
      restored="$(curl -fsS "http://127.0.0.1:$OLD_PORT/fleet/status" | python3 -c 'import json,sys;print(next((m["state"] for m in json.load(sys.stdin)["instances"] if m["name"]==sys.argv[1]),"missing"))' "$RELEASE_VM")" || restored=unknown
      [[ "$restored" == running ]] && break
      sleep 1
    done
    [[ "$restored" == running ]] || result=1
  fi
  if (( WATCHDOG_PAUSED )); then
    launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$WATCHDOG.plist" || result=1
  fi
  trap - EXIT
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

echo '== diff and verify stopped snapshots =='
if [[ ! -f "$OUT/delta.sqlite" ]]; then
  python3 "$HERE/shared_base.py" diff "$BASE_BUNDLE/Disk.img" "$MACHINE_BUNDLE/Disk.img" "$OUT"
fi
VOS_EXP_RECEIPT="$OUT/base-delta-verify.json" python3 "$HERE/shared_base.py" verify "$BASE_BUNDLE/Disk.img" "$MACHINE_BUNDLE/Disk.img" "$OUT"
python3 - "$BASE_BUNDLE/Disk.img" "$MACHINE_BUNDLE/Disk.img" "$OUT/source-pins.json" <<'PY'
import json, os, sys
pins = {}
for path in sys.argv[1:3]:
    info = os.stat(path)
    pins[path] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
open(sys.argv[3], "w").write(json.dumps(pins))
PY
mkdir -p "$FLEET_HOME/shared-disks"
python3 - "$FLEET_HOME" "$BASE_BUNDLE" "$MACHINE_BUNDLE" "$NAME" "$OUT/delta.sqlite" "$NBD_PORT" <<'PY'
import json, pathlib, sys
root, base, machine, name, delta, port = sys.argv[1:]
root = pathlib.Path(root).resolve()
for source in (base, machine):
    if pathlib.Path(source).resolve().parent != root:
        raise SystemExit("snapshot bundles must be inside the isolated fleet root")
config = {"baseName": pathlib.Path(base).stem, "platformName": pathlib.Path(machine).stem,
          "delta": str(pathlib.Path(delta).resolve()), "port": int(port)}
path = root / "shared-disks" / (name + ".json")
if path.exists() and json.loads(path.read_text()) != config:
    raise SystemExit("refusing to replace existing shared-disk configuration")
path.write_text(json.dumps(config, indent=2))
PY
export VOS_FLEET_HOME="$FLEET_HOME" VOS_FLEET_TOOLS="$FLEET_TOOLS"
export VOS_SUP_PORT="$HTTP_PORT" VOS_FLEET_HTTP_PORT="$HTTP_PORT"
export VOS_FLEET_BASE_PORT=9160 VOS_FLEET_AGENT_LABEL="$LABEL"
export VOS_DESKTOP_MIN_BYTES="${VOS_DESKTOP_MIN_BYTES:-30000}"
bash "$FLEET_TOOLS/fleet/runbook.sh" live "$NAME" --prepare-only

if [[ -n "$RELEASE_VM" ]]; then
  echo "== temporarily release fleet slot: $RELEASE_VM =="
  if launchctl print "$DOMAIN/$WATCHDOG" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$WATCHDOG"
    WATCHDOG_PAUSED=1
  fi
  RELEASED=1
  python3 "$FLEET_TOOLS/fleet/fleet.py" --supervisor "http://127.0.0.1:$OLD_PORT" stop "$RELEASE_VM"
  for _ in $(seq 1 30); do
    state="$(curl -fsS "http://127.0.0.1:$OLD_PORT/fleet/status" | python3 -c 'import json,sys;print(next((m["state"] for m in json.load(sys.stdin)["instances"] if m["name"]==sys.argv[1]),"missing"))' "$RELEASE_VM")"
    [[ "$state" == idle ]] && break
    sleep 1
  done
  [[ "$state" == idle ]] || { echo fleet_slot_not_released >&2; exit 1; }
fi

echo '== managed shared-disk guest boot =='
STARTED=1
bash "$FLEET_TOOLS/fleet/runbook.sh" live "$NAME" --boot-only
bash "$FLEET_TOOLS/fleet/runbook.sh" desktop "$NAME" "$TMO"
cp "/tmp/vos-$NAME-desktop.jpg" "$OUT/boot-1.jpg"
bash "$FLEET_TOOLS/fleet/runbook.sh" live "$NAME" --boot-only
python3 "$FLEET_TOOLS/fleet/fleet.py" --supervisor "http://127.0.0.1:$HTTP_PORT" stop "$NAME"
sleep 3
echo '== restart the disk service and boot from the retained overlay =='
launchctl bootout "$DOMAIN/com.virtualos.disk.$NAME"
sleep 1
bash "$FLEET_TOOLS/fleet/runbook.sh" live "$NAME" --boot-only
bash "$FLEET_TOOLS/fleet/runbook.sh" desktop "$NAME" "$TMO"
cp "/tmp/vos-$NAME-desktop.jpg" "$OUT/boot-2.jpg"
python3 "$FLEET_TOOLS/fleet/fleet.py" --supervisor "http://127.0.0.1:$HTTP_PORT" stop "$NAME"
sleep 3
python3 - "$FLEET_HOME" "$NAME" "$OUT" <<'PY'
import hashlib, json, os, pathlib, sqlite3, sys, time
sys.path.insert(0, os.environ["VOS_FLEET_TOOLS"])
from cloud_disk_r2_delegate import fingerprint
root, name, out = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
log = root / ".virtualos-nbd" / name / "server.log"
sessions = []
ready = []
for line in log.read_text().splitlines():
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if "base_delta_nbd_connection" in row and row["base_delta_nbd_connection"]["reads"]:
        sessions.append(row["base_delta_nbd_connection"])
    if row.get("base_delta_listener") == "ready":
        ready.append(row)
assert len(sessions) >= 2, "two completed guest boots required"
assert len(ready) >= 2 and ready[-1]["delta_seeded"] == 0, "restart reseeded guest storage"
for path, pin in json.loads((out / "source-pins.json").read_text()).items():
    info = os.stat(path)
    assert [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns] == pin, "source disk changed"
frame = pathlib.Path("/tmp/vos-" + name + "-desktop.jpg")
assert frame.stat().st_size > int(os.environ["VOS_DESKTOP_MIN_BYTES"])
boot_frames = []
for number in (1, 2):
    captured = out / ("boot-%d.jpg" % number)
    data = captured.read_bytes()
    assert len(data) > int(os.environ["VOS_DESKTOP_MIN_BYTES"])
    boot_frames.append({"boot": number, "file": captured.name, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
overlay = root / ".virtualos-nbd" / name / "overlay.sqlite"
with sqlite3.connect(overlay) as db:
    blocks = db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
receipt = {"managed_shared_disk_boot": "verified", "verified_at_unix": int(time.time()),
           "machine": name, "local_machine_disk": "absent", "writes_to_base": 0,
           "guest_boots_verified": 2, "disk_service_restart_verified": True,
           "frame_size_gate_bytes": int(os.environ["VOS_DESKTOP_MIN_BYTES"]),
           "restart_blocks_reseeded": ready[-1]["delta_seeded"], "source_disks_unchanged": True,
           "frame_bytes": frame.stat().st_size, "frame_sha256": hashlib.sha256(frame.read_bytes()).hexdigest(),
           "boot_frames": boot_frames,
           "nbd_reads": sum(s["reads"] for s in sessions), "nbd_writes": sum(s["writes"] for s in sessions),
           "nbd_metrics_scope": "retained server log",
           "overlay_blocks": blocks, "overlay_allocated_bytes": overlay.stat().st_blocks * 512,
           "initial_delta_bytes": json.loads((out / "base-delta-verify.json").read_text())["delta_bytes"],
           "initial_delta_sha256": fingerprint(out / "delta.sqlite")["source_delta_sha256"]}
assert not (root / (name + ".bundle") / "Disk.img").exists()
assert all(s["negotiated"] for s in sessions)
assert sum(s["metrics"]["read"]["errors"] + s["metrics"]["write"]["errors"] for s in sessions) == 0
(out / "managed-boot-receipt.json").write_text(json.dumps(receipt, indent=2))
print(json.dumps(receipt, sort_keys=True))
PY
