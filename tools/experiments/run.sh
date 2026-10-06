#!/usr/bin/env bash
# Shared-base fleet disk experiment runner (ISOLATED — see README.md).
# Reads only; never touches the production machine store or the stable disk plane.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
BASE="${VOS_EXP_BASE:-$FLEET_HOME/vm-clone.bundle/Disk.img}"
MACHINES="${VOS_EXP_MACHINES:-$FLEET_HOME/vm-clone2.bundle/Disk.img}"
OUT="${VOS_EXP_OUT:-/private/tmp/vos-exp-shared-base}"
LIMIT="${VOS_EXP_LIMIT_BLOCKS:-}"

mkdir -p "$OUT"
echo "== shared-base report =="
args=(report "$BASE" $MACHINES)
[[ -n "$LIMIT" ]] && args+=(--limit-blocks "$LIMIT")
python3 "$HERE/shared_base.py" "${args[@]}"

echo "== materialise per-machine delta vs base =="
first_machine=$(echo $MACHINES | awk '{print $1}')
python3 "$HERE/shared_base.py" diff "$BASE" "$first_machine" "$OUT/delta-$first_machine" \
  ${LIMIT:+--limit-blocks "$LIMIT"} 2>/dev/null || \
python3 "$HERE/shared_base.py" diff "$BASE" "$first_machine" "$OUT/delta" \
  ${LIMIT:+--limit-blocks "$LIMIT"}

echo "== artefacts =="
ls -la "$OUT" | sed -n '1,20p'
