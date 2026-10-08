#!/usr/bin/env bash
# Shared-base fleet disk experiment runner (ISOLATED — see README.md).
# Reads only; never touches the production machine store or the stable disk plane.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
BASE="${VOS_EXP_BASE:-$FLEET_HOME/vm-a.bundle/Disk.img}"
MACHINES="${VOS_EXP_MACHINES:-$FLEET_HOME/vm-clone2.bundle/Disk.img}"
OUT="${VOS_EXP_OUT:-/private/tmp/vos-exp-shared-base}"
LIMIT="${VOS_EXP_LIMIT_BLOCKS:-}"

echo "== shared-base selftest (synthetic, always runs) =="
python3 "$HERE/shared_base.py" selftest

if [[ ! -f "$BASE" ]]; then
  echo
  echo "no base disk at $BASE — selftest passed; set VOS_EXP_BASE to a real"
  echo "clone (never ~/VirtualOSMachines) to measure and verify a real machine."
  exit 0
fi

mkdir -p "$OUT"
echo "== shared-base report =="
args=(report "$BASE" $MACHINES)
[[ -n "$LIMIT" ]] && args+=(--limit-blocks "$LIMIT")
python3 "$HERE/shared_base.py" "${args[@]}"

echo "== materialise per-machine delta vs base =="
first_machine=$(echo $MACHINES | awk '{print $1}')
machine_label="$(basename "$(dirname "$first_machine")")"
DELTA="$OUT/delta-$machine_label"
args=(diff "$BASE" "$first_machine" "$DELTA")
[[ -n "$LIMIT" ]] && args+=(--limit-blocks "$LIMIT")
python3 "$HERE/shared_base.py" "${args[@]}"

echo "== prove base + delta reconstructs the machine byte-for-byte =="
args=(verify "$BASE" "$first_machine" "$DELTA")
[[ -n "$LIMIT" ]] && args+=(--limit-blocks "$LIMIT")
python3 "$HERE/shared_base.py" "${args[@]}"

echo "== artefacts =="
ls -la "$OUT" | sed -n '1,20p'
