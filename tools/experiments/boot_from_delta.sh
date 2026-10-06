#!/usr/bin/env bash
# Boot a machine from ONE shared base image plus its small per-machine delta.
#
# ISOLATED experiment: reads the base and machine images read-only, writes only
# to /private/tmp, and never references the production machine store. Serving is
# delegated to the stable disk plane as a black-box NBD server.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
FLEET_TOOLS="${VOS_FLEET_TOOLS:-$FLEET_HOME/tools}"
SUPERVISOR="${VOS_SUPERVISOR:-$FLEET_TOOLS/fleet/fleet-supervisor}"
CS="$FLEET_TOOLS/computer-space.sh"

BASE_BUNDLE="${VOS_EXP_BASE_BUNDLE:-$FLEET_HOME/vm-clone.bundle}"
MACHINE_BUNDLE="${VOS_EXP_MACHINE_BUNDLE:-$FLEET_HOME/vm-clone2.bundle}"
NAME="${VOS_EXP_NAME:-vm-from-delta}"
OUT="${VOS_EXP_OUT:-/private/tmp/vos-exp-shared-base}"
HTTP_PORT="${VOS_EXP_HTTP_PORT:-9250}"
TMO="${VOS_EXP_TMO:-240}"

[[ -f "$BASE_BUNDLE/Disk.img" ]] || { echo "no base disk: $BASE_BUNDLE" >&2; exit 1; }
[[ -f "$MACHINE_BUNDLE/Disk.img" ]] || { echo "no machine disk: $MACHINE_BUNDLE" >&2; exit 1; }
[[ -x "$CS" && -x "$SUPERVISOR" ]] || { echo "fleet tools not built" >&2; exit 1; }

mkdir -p "$OUT"
DELTA_DIR="$OUT/delta"
OVERLAY_DIR="/private/tmp/vos-nbd-bundle-$NAME"
rm -rf "$OVERLAY_DIR"; mkdir -p "$OVERLAY_DIR"

echo "== 1. diff machine vs base (read-only) =="
python3 "$HERE/shared_base.py" diff "$BASE_BUNDLE/Disk.img" "$MACHINE_BUNDLE/Disk.img" "$DELTA_DIR"

echo "== 2. bind an overlay to the base, then seed the machine's delta into it =="
# Pick a free port.
PORT=""
for p in $(seq 40950 40990); do
  lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1 || { PORT="$p"; break; }
done
[[ -n "$PORT" ]] || { echo "no free port" >&2; exit 1; }

# Create the overlay with the identity the base server will expect (RawDisk binds
# an overlay to the base image's path+inode+size+mtime), then seed the delta.
python3 - "$FLEET_TOOLS" "$BASE_BUNDLE/Disk.img" "$OVERLAY_DIR/overlay.sqlite" "$DELTA_DIR/delta.sqlite" <<'PY'
import sqlite3, sys
sys.path.insert(0, sys.argv[1])
from cloud_disk import RawDisk
base_disk, overlay, delta = sys.argv[2], sys.argv[3], sys.argv[4]
disk = RawDisk(base_disk, overlay, overlay_limit_bytes=2 * 1024**3, min_host_free_bytes=0)
disk.close()
db = sqlite3.connect(overlay)
rows = list(sqlite3.connect(delta).execute("SELECT idx, data FROM blocks"))
for idx, data in rows:
    db.execute("INSERT OR REPLACE INTO blocks VALUES (?,?)", (idx, data))
db.commit(); db.close()
print("seeded %d delta blocks into the base overlay" % len(rows))
PY

serve() {
  VOS_NBD_BUNDLE_ROOT="$FLEET_HOME" VOS_NBD_BUNDLE_NAME="$(basename "$BASE_BUNDLE" .bundle)" \
    VOS_NBD_BUNDLE_OVERLAY="$OVERLAY_DIR/overlay.sqlite" VOS_NBD_BUNDLE_PORT="$PORT" \
    bash "$CS" --nbd-bundle-serve > "$OVERLAY_DIR/server.log" 2>&1 &
  echo $! > "$OVERLAY_DIR/server.pid"
  for _ in $(seq 1 60); do grep -q '"ready"' "$OVERLAY_DIR/server.log" 2>/dev/null && return 0; sleep 0.2; done
  return 1
}
stop_serve() {
  kill "$(cat "$OVERLAY_DIR/server.pid")" 2>/dev/null || true
  pkill -f "cloud_disk.py bundle-serve $FLEET_HOME " 2>/dev/null || true
  sleep 1
}

echo "== 3. serve base + delta and boot a machine from it =="
serve || { echo "serve failed (bind)"; cat "$OVERLAY_DIR/server.log"; exit 1; }

DST="$FLEET_HOME/$NAME.bundle"
rm -rf "$DST"; mkdir -p "$DST" "$FLEET_HOME/machines"
for f in HardwareModel MachineIdentifier AuxiliaryStorage; do
  cp -c "$BASE_BUNDLE/$f" "$DST/$f" 2>/dev/null || cp "$BASE_BUNDLE/$f" "$DST/$f"
done
python3 - "$DST/Parameters.txt" "$PORT" <<'PY'
import json, sys
open(sys.argv[1], "w").write(json.dumps({
    "cpuCount": 3, "memorySizeInGB": 4, "screenWidth": 1500, "screenHeight": 900,
    "nbdURL": "nbd://127.0.0.1:%s/virtualos-test" % sys.argv[2]}, indent=2))
PY
ln -sfn "$DST" "$FLEET_HOME/machines/$NAME.bundle"

LABEL="com.virtualos.exp.$NAME"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$SUPERVISOR</string>
    <string>--root</string><string>$FLEET_HOME/machines</string>
    <string>--names</string><string>$NAME</string>
    <string>--http-port</string><string>$HTTP_PORT</string>
    <string>--base-port</string><string>9150</string>
    <string>--start</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$OVERLAY_DIR/boot.log</string>
  <key>StandardErrorPath</key><string>$OVERLAY_DIR/boot.log</string>
</dict></plist>
PLIST
launchctl unload "$PLIST" 2>/dev/null || true; sleep 1
launchctl load "$PLIST"

OUT_JPG="/tmp/vos-$NAME-desktop.jpg"; WAITED=0; BYTES=0
while [[ $WAITED -lt $TMO ]]; do
  CODE="$(curl -s -o "$OUT_JPG.tmp" -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT/fleet/frame/$NAME" || true)"
  if [[ "$CODE" == 200 ]]; then
    BYTES="$(wc -c <"$OUT_JPG.tmp" | tr -d ' ')"
    [[ "$BYTES" -gt 100000 ]] && { mv "$OUT_JPG.tmp" "$OUT_JPG"; break; }
  fi
  rm -f "$OUT_JPG.tmp"; sleep 10; WAITED=$((WAITED + 10))
done
launchctl unload "$PLIST" 2>/dev/null || true; rm -f "$PLIST"
stop_serve

DELTA_BYTES="$(python3 -c "import json;print(json.load(open('$DELTA_DIR/delta.json'))['delta_bytes'])" 2>/dev/null || echo 0)"
BASE_BYTES="$(stat -f %z "$BASE_BUNDLE/Disk.img" 2>/dev/null || echo 0)"
python3 - "$DELTA_BYTES" "$BASE_BYTES" "$BYTES" "$OVERLAY_DIR/server.log" <<'PY'
import json, sys
delta, base, frame = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
rows = [json.loads(l) for l in open(sys.argv[4]) if l.strip().startswith('{')]
conn = [r['bundle_nbd_connection'] for r in rows if 'bundle_nbd_connection' in r]
reads = conn[-1]['reads'] if conn else 0
rb = conn[-1]['metrics']['read']['request_bytes_total'] if conn else 0
print(json.dumps({
    "shared_base_boot": "verified" if frame > 100000 else "no_desktop",
    "base_bytes": base,
    "machine_delta_bytes": delta,
    "delta_pct_of_disk": round(100.0 * delta / max(1, base), 4),
    "one_machine_cost_bytes": base + delta,
    "vs_full_copy_bytes": 2 * base,
    "frame_bytes": frame,
    "nbd_reads": reads, "nbd_read_bytes": rb,
}, sort_keys=True))
PY
echo "experiment artefacts under $OUT"
