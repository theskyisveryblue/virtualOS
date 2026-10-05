#!/usr/bin/env bash
# End-to-end self-test for the experimental virtualOS fleet box.
#
# Validates:
#   1. supervisor compiles
#   2. nested-virtualization capability is reported
#   3. supervisor HTTP surface responds (health/status/capability)
#   4. multiplexer fans out to the supervisor
#   5. per-VM start/stop routes are accepted (no bundles required to be bootable)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FLEET_DIR="$ROOT/tools/fleet"
BIN="$FLEET_DIR/fleet-supervisor"
PY="$FLEET_DIR/fleet.py"
HTTP_PORT="${VOS_FLEET_HTTP_PORT:-9210}"
MUX_PORT="${VOS_FLEET_MUX_PORT:-9310}"
SUP_URL="http://127.0.0.1:${HTTP_PORT}"
MUX_URL="http://127.0.0.1:${MUX_PORT}"
TMP_ROOT="$(mktemp -d)"
trap 'cleanup' EXIT

cleanup() {
  [[ -n "${SP:-}" ]] && kill "$SP" 2>/dev/null || true
  [[ -n "${MP:-}" ]] && kill "$MP" 2>/dev/null || true
  rm -rf "$TMP_ROOT"
}

pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; exit 1; }

echo "[1/6] build supervisor"
swiftc -O -framework Virtualization -framework AppKit "$FLEET_DIR/FleetSupervisor.swift" -o "$BIN" >/dev/null 2>&1
[[ -x "$BIN" ]] && pass "compiled" || fail "compile"

# Fake bundles so the supervisor has something to enumerate.
mkdir -p "$TMP_ROOT/vm-a.bundle" "$TMP_ROOT/vm-b.bundle"

echo "[2/6] launch supervisor"
"$BIN" --root "$TMP_ROOT" --names vm-a,vm-b --http-port "$HTTP_PORT" >/tmp/vos-fleet-selftest-sup.log 2>&1 &
SP=$!
sleep 1
curl -sf "$SUP_URL/fleet/health" >/dev/null && pass "health" || fail "supervisor health"

echo "[3/6] nested virtualization capability"
CAP="$(curl -sf "$SUP_URL/fleet/capability")"
echo "    $CAP"
echo "$CAP" | grep -q '"ok":true' && pass "capability reported" || fail "capability"

echo "[4/6] multiplexer"
python3 "$PY" --supervisor "$SUP_URL" serve --port "$MUX_PORT" >/tmp/vos-fleet-selftest-mux.log 2>&1 &
MP=$!
sleep 1
curl -sf "$MUX_URL/health" >/dev/null && pass "mux health" || fail "mux health"
curl -sf "$MUX_URL/fleet/status" | grep -Eq '"count": ?2' && pass "mux fans out status" || fail "mux status"

echo "[5/6] per-VM control routes"
python3 "$PY" --supervisor "$SUP_URL" start vm-a | grep -Eq '"ok": ?true' && pass "start vm-a accepted" || fail "start vm-a"
python3 "$PY" --supervisor "$SUP_URL" stop vm-a | grep -Eq '"ok": ?true' && pass "stop vm-a accepted" || fail "stop vm-a"

echo "[6/6] frame endpoint"
CODE_SUP="$(curl -s -o /dev/null -w '%{http_code}' "$SUP_URL/fleet/frame/vm-a")"
[[ "$CODE_SUP" == "503" ]] && pass "supervisor frame route (503 no running vm)" || fail "supervisor frame route ($CODE_SUP)"
CODE_MUX="$(curl -s -o /dev/null -w '%{http_code}' "$MUX_URL/fleet/frame/vm-a")"
[[ "$CODE_MUX" == "503" ]] && pass "mux frame proxy (503 no running vm)" || fail "mux frame proxy ($CODE_MUX)"

echo "ALL FLEET SELF-TESTS PASSED"
