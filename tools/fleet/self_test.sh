#!/usr/bin/env bash
# End-to-end self-test for the experimental virtualOS fleet box.
#
# Validates:
#   1. supervisor compiles
#   2. nested-virtualization capability is reported
#   3. supervisor HTTP surface responds (health/status/capability)
#   4. multiplexer fans out to the supervisor
#   5. per-VM start/stop routes are accepted (no bundles required to be bootable)
#   6. frame endpoints are routed (supervisor + mux)
#   7. per-VM input verbs are routed and bad bodies rejected
#   8. the brain action normalizer validates key/type/click offline
#   9. the fleet_push control loop survives a transient failure
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FLEET_DIR="$ROOT/tools/fleet"
PY="$FLEET_DIR/fleet.py"
HTTP_PORT="${VOS_FLEET_HTTP_PORT:-9210}"
MUX_PORT="${VOS_FLEET_MUX_PORT:-9310}"
SUP_URL="http://127.0.0.1:${HTTP_PORT}"
MUX_URL="http://127.0.0.1:${MUX_PORT}"
TMP_ROOT="$(mktemp -d)"
# Build a SCRATCH supervisor. Never the production binary: overwriting the
# signed supervisor with an unsigned build strips its virtualization entitlement
# and the live stack fails to restart.
BIN="$TMP_ROOT/fleet-supervisor-selftest"
trap 'cleanup' EXIT

cleanup() {
  [[ -n "${SP:-}" ]] && kill "$SP" 2>/dev/null || true
  [[ -n "${MP:-}" ]] && kill "$MP" 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$TMP_ROOT"
}

pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; exit 1; }

echo "[1/10] build supervisor"
swiftc -O -framework Virtualization -framework AppKit "$FLEET_DIR/FleetSupervisor.swift" -o "$BIN" >/dev/null 2>&1
[[ -x "$BIN" ]] && pass "compiled" || fail "compile"

# Fake bundles so the supervisor has something to enumerate.
mkdir -p "$TMP_ROOT/vm-a.bundle" "$TMP_ROOT/vm-b.bundle"

echo "[2/10] launch supervisor"
"$BIN" --root "$TMP_ROOT" --names vm-a,vm-b --http-port "$HTTP_PORT" >/tmp/vos-fleet-selftest-sup.log 2>&1 &
SP=$!
sleep 1
curl -sf "$SUP_URL/fleet/health" >/dev/null && pass "health" || fail "supervisor health"

echo "[3/10] nested virtualization capability"
CAP="$(curl -sf "$SUP_URL/fleet/capability")"
echo "    $CAP"
echo "$CAP" | grep -q '"ok":true' && pass "capability reported" || fail "capability"

echo "[4/10] multiplexer"
python3 "$PY" --supervisor "$SUP_URL" serve --port "$MUX_PORT" >/tmp/vos-fleet-selftest-mux.log 2>&1 &
MP=$!
sleep 1
curl -sf "$MUX_URL/health" >/dev/null && pass "mux health" || fail "mux health"
curl -sf "$MUX_URL/fleet/status" | grep -Eq '"count": ?2' && pass "mux fans out status" || fail "mux status"

echo "[5/10] per-VM control routes"
python3 "$PY" --supervisor "$SUP_URL" start vm-a | grep -Eq '"ok": ?true' && pass "start vm-a accepted" || fail "start vm-a"
python3 "$PY" --supervisor "$SUP_URL" stop vm-a | grep -Eq '"ok": ?true' && pass "stop vm-a accepted" || fail "stop vm-a"

echo "[6/10] frame endpoint"
CODE_SUP="$(curl -s -o /dev/null -w '%{http_code}' "$SUP_URL/fleet/frame/vm-a")"
[[ "$CODE_SUP" == "503" ]] && pass "supervisor frame route (503 no running vm)" || fail "supervisor frame route ($CODE_SUP)"
CODE_MUX="$(curl -s -o /dev/null -w '%{http_code}' "$MUX_URL/fleet/frame/vm-a")"
[[ "$CODE_MUX" == "503" ]] && pass "mux frame proxy (503 no running vm)" || fail "mux frame proxy ($CODE_MUX)"

echo "[7/10] per-VM input route (accepts the whole verb surface)"
for body in \
  '{"action":"click","nx":0.5,"ny":0.5}' \
  '{"action":"move","nx":0.1,"ny":0.9}' \
  '{"action":"key","key":"return"}' \
  '{"action":"type","text":"hello world"}'; do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    -d "$body" "$SUP_URL/fleet/input/vm-a")"
  # vm-a is not running here, so input is refused (503) but MUST be routed (not 404).
  [[ "$CODE" == "503" || "$CODE" == "200" ]] || fail "input route for $body returned $CODE"
done
CODE_BAD="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d 'not json' "$SUP_URL/fleet/input/vm-a")"
[[ "$CODE_BAD" == "400" ]] && pass "input verbs routed, bad body rejected (400)" || fail "bad body returned $CODE_BAD"

echo "[8/10] brain action normalizer (offline, no model)"
python3 - "$FLEET_DIR/brain.py" <<'PY' && pass "normalizer validates key/type/click" || fail "brain normalizer"
import importlib.util, json, sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location("brain", path)
brain = importlib.util.module_from_spec(spec)
sys.modules["brain"] = brain
spec.loader.exec_module(brain)
norm = brain._ollama_action
class FakeFrame:
    jpeg = b""; width = 100; height = 100; likely_blank = False
    sha256 = "x"
def make(fake):
    orig = brain._http
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"message": {"content": json.dumps(fake)}}).encode()
    brain._http = lambda *a, **k: Resp()
    try:
        return norm(FakeFrame(), "t", "m", "http://x")
    finally:
        brain._http = orig
cases = [
    ({"type": "key", "key": "return"}, "key"),
    ({"type": "key", "key": "Escape"}, "key"),
    ({"type": "type", "text": "hello"}, "type"),
    ({"type": "click", "point_2d": [100, 200]}, "click"),
]
for fake, expected in cases:
    out = make(fake)
    if out.get("type") != expected:
        raise SystemExit("type mismatch %s -> %s" % (fake, out))
for bad in ({"type": "key", "key": "x"*40}, {"type": "type", "text": ""},
            {"type": "click", "point_2d": [5000, 0]}):
    if make(bad).get("type") != "abstain":
        raise SystemExit("should have abstained: %s" % bad)
PY

echo "[9/10] fleet_push control loop survives a transient failure (offline)"
python3 - "$FLEET_DIR/fleet_push.py" <<'PY' && pass "control loop is unkillable" || fail "control loop"
import importlib.util, threading, sys, time
path = sys.argv[1]
spec = importlib.util.spec_from_file_location("fleet_push", path)
fp = importlib.util.module_from_spec(spec)
sys.modules["fleet_push"] = fp
spec.loader.exec_module(fp)
calls = {"n": 0}
def boom(*a, **k):
    calls["n"] += 1
    raise RuntimeError("transient relay/proxy error")
fp.control_once = boom
thread = threading.Thread(target=fp.control_loop,
                          args=("http://x", "http://y", "tok", False), daemon=True)
thread.start()
time.sleep(3.0)
if not thread.is_alive() or calls["n"] < 2:
    raise SystemExit("control_loop died or did not retry (alive=%s, tries=%d)"
                     % (thread.is_alive(), calls["n"]))
PY

echo "[10/10] shared-base disk: base + delta round-trips (offline)"
SHARED_BASE="$FLEET_DIR/../experiments/shared_base.py"
if [[ -f "$SHARED_BASE" ]]; then
  python3 "$SHARED_BASE" selftest 2>/dev/null \
    | grep -q '"shared_base_selftest": "passed"' \
    && pass "shared base + delta reconstructs byte-for-byte" \
    || fail "shared base round trip"
else
  fail "shared_base.py not synced to fleet (expected $SHARED_BASE)"
fi

echo "ALL FLEET SELF-TESTS PASSED"
