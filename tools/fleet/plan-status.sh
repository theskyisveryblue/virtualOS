#!/usr/bin/env bash
# plan-status.sh — the truth scoreboard.
#
# Runs every measured lab and prints the whole state of the project as numbers,
# not claims. This is the "single command that prints the number" the plan
# promises. It is read-only against the stable planes and never starts a VM
# unless a check explicitly needs one.
#
#   tools/fleet/plan-status.sh            # full scoreboard
#   tools/fleet/plan-status.sh --quick    # skip anything slow/local-only
#
# It is RELAY-FIRST: the operator Mac can roam off the Mini's LAN and the cloud
# relay still carries the fleet truth. Only data that truly lives on the Mini
# (the delta experiment, the Linux console log, the self-test binary) needs SSH,
# and that probe runs once and degrades to "not reachable from here".
#
# Exit code is 0 even when a pillar is "blocked" — a blocked hard truth is a
# result, not a failure. The point is to see reality.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
FLEET_HOME="${VOS_FLEET_HOME:-$HOME/vos-fleet}"
QUICK="${1:-}"
HOST="$(hostname -s 2>/dev/null || echo local)"
MINI="${VOS_MINI_HOST:-mac-mini-ai}"

line() { printf '%-26s %s\n' "$1" "$2"; }
sep()  { printf -- '----------------------------------------------------------------\n'; }
host_probe_status() {
  if [[ "$QUICK" == --quick ]]; then
    printf 'not checked (--quick)'
  else
    printf 'SSH unavailable from here'
  fi
}

relay_status_json() {
  python3 "$HERE/relay_read.py"
}

# Read one dotted field out of a JSON blob passed as the first argument.
json_get() {
  python3 -c '
import json, sys
try:
    node = json.loads(sys.argv[2])
    for key in sys.argv[1].split("."):
        node = node[int(key)] if key.isdigit() else node[key]
    print(node)
except Exception:
    pass
' "$1" "${2:-}" 2>/dev/null || true
}

RELAY_URL="${RELAY_URL:-https://virtualos-relay.ai-c24.workers.dev}"
RELAY_BODY="$(relay_status_json 2>/dev/null || true)"

# The Mini may be off this host's LAN (operator roaming). Probe once, quietly.
mini_reachable() {
  [[ "$QUICK" == "--quick" ]] && return 1
  ssh -o BatchMode=yes -o ConnectTimeout=3 "$MINI" true >/dev/null 2>&1
}
MINI_UP=0; mini_reachable && MINI_UP=1
mini_run() { [[ "$MINI_UP" == "1" ]] || return 1; ssh -o BatchMode=yes -o ConnectTimeout=4 "$MINI" "$1" 2>/dev/null; }
# True when this host IS the fleet host (the Mini), so files we normally fetch
# over SSH are read locally instead. Detected a few ways because the Mini's
# hostname (e.g. ais-Mac-mini) does not equal its SSH alias (mac-mini-ai): a
# 'mini' in the hostname, the fleet home present locally, or the supervisor
# already running here all mean "we are the host".
ON_MINI=0
# macOS ships bash 3.2, which has no ${var,,} lowercase expansion; use tr.
case "$(printf '%s' "$HOST" | tr '[:upper:]' '[:lower:]')" in *mini*) ON_MINI=1 ;; esac
[[ -d "$FLEET_HOME/tools/fleet" ]] && ON_MINI=1
# Match the actual supervisor binary, not any process with "fleet-supervisor"
# in its command line (e.g. an unrelated clone-fleet-supervisors.py).
pgrep -f "[f]leet-supervisor --root" >/dev/null 2>&1 && ON_MINI=1
read_host() { # read a file that lives on the fleet host, local or remote
  if [[ "$ON_MINI" == "1" ]]; then
    local path="${1/#\~/$HOME}"   # expand a leading ~ for the local read
    [[ -f "$path" ]] && cat "$path" 2>/dev/null || true
  else
    mini_run "cat $1 2>/dev/null" || true
  fi
}

echo "virtualOS plan-status  host=$HOST  $(date '+%Y-%m-%d %H:%M:%S')"
sep

# --- P0: is the live stack actually live? ---------------------------------
echo "[P0] live stack"
SUP_URL="${VOS_SUP_URL:-http://127.0.0.1:9225}"
sup_body="$(curl -s -m 3 "$SUP_URL/fleet/status" 2>/dev/null || true)"
sup_src=""
if ! printf '%s' "$sup_body" | python3 -c 'import sys,json;json.load(sys.stdin)' >/dev/null 2>&1; then
  sup_body=""
fi
if [[ -z "$sup_body" ]]; then
  sup_body="$(mini_run "curl -s -m 3 http://127.0.0.1:9225/fleet/status" || true)"
  [[ -n "$sup_body" ]] && sup_src=" (mini)"
fi
# Relay-first: the relay carries the fleet the host pushed. It is the truth that
# survives the operator leaving the Mini's LAN.
if [[ -z "$sup_body" && -n "$RELAY_BODY" ]]; then
  sup_body="$(printf '%s' "$RELAY_BODY" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["fleet"]))' 2>/dev/null || true)"
  [[ -n "$sup_body" ]] && sup_src=" (via relay)"
fi
if printf '%s' "$sup_body" | python3 -c 'import sys,json;json.load(sys.stdin)' >/dev/null 2>&1; then
  n="$(json_get count "$sup_body")"
  states="$(printf '%s' "$sup_body" | python3 -c 'import sys,json
d=json.load(sys.stdin)
print(",".join("%s:%s"%(i.get("name"),i.get("state")) for i in d.get("instances",[])))' 2>/dev/null || true)"
  line "supervisor" "up${sup_src} vms=${n:-0} ${states:+[$states]}"
else
  line "supervisor" "DOWN — run: runbook.sh live"
fi
if [[ -n "$RELAY_BODY" ]]; then
  q="$(printf '%s' "$RELAY_BODY" | python3 -c 'import sys,json;d=json.load(sys.stdin);l=d["liveness"];print(("live" if not l["isStale"] else "STALE"), "hb_age=%ss" % l["lastHeartbeatAgeSec"])' 2>/dev/null || echo "unparseable")"
  line "relay" "$q"
elif [[ -f "$HOME/.config/virtualos/relay.env" ]]; then
  line "relay" "unreachable or unauthorized at ${RELAY_URL:-?}"
else
  line "relay" "no relay.env"
fi
# The watchdog that keeps it live. It runs on the host, so probe there.
if [[ "$MINI_UP" == "1" ]]; then
  wd="$(mini_run 'launchctl list 2>/dev/null | grep -q com.virtualos.fleet-watchdog && echo "on (mini)" || echo "off"' || echo "off")"
  line "watchdog" "$wd"
fi

# --- P1: storage / compact fleet ------------------------------------------
echo; echo "[P1] storage"
[[ -f "$ROOT/tools/cloud_disk.py" ]] && line "disk plane" "present (cloud_disk.py)"
# Run the real storage self-test so this pillar prints a number, not a claim.
# Skipped under --quick because it does disk I/O.
if [[ "$QUICK" != "--quick" && -x "$ROOT/tools/computer-space.sh" ]]; then
  cd_json="$(bash "$ROOT/tools/computer-space.sh" --cloud-disk-self-test 2>/dev/null || true)"
  st="$(printf '%s' "$cd_json" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("cloud_disk_self_test","?"))' 2>/dev/null || echo "?")"
  line "storage self-test" "${st} (checksum+overlay+reconnect)"
fi
r2cfg="$HOME/.config/virtualos/r2-mac-config.json"
if [[ -f "$r2cfg" ]]; then
  line "r2 config" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["object_prefix"])' "$r2cfg" 2>/dev/null)"
fi
# The headline experiment result is produced where the clones live (the Mini).
delta_json="$(read_host /private/tmp/vos-exp-shared-base/delta/delta.json)"
if [[ "$ON_MINI" == "1" ]]; then delta_src=""; else delta_src=" [mini]"; fi
if [[ -n "$delta_json" ]]; then
  python3 - "$delta_json" "$delta_src" <<'PY' 2>/dev/null || line "shared base+delta" "unparseable"
import json, sys
d = json.loads(sys.argv[1])
print("%-26s delta=%.1f MB (%.2f%% of disk)%s" % ("shared base+delta", d["delta_bytes"]/1e6, d["delta_pct"], sys.argv[2]))
PY
elif [[ "$MINI_UP" == "0" ]]; then
  line "shared base+delta" "$(host_probe_status)"
else
  line "shared base+delta" "not measured — run experiments/run.sh on the Mini"
fi
# The round-trip receipt: proves base + delta reconstructs a machine, not just
# that the delta is small. Written by experiments/shared_base.py verify.
receipt=""
[[ -f "$ROOT/tools/experiments/shared-base-receipt.json" ]] && receipt="$(cat "$ROOT/tools/experiments/shared-base-receipt.json")"
# On the Mini the receipt is local (read above if committed); on the operator Mac
# fetch it from the Mini's fleet home, using a literal path (not the Mac's $HOME).
[[ -n "$receipt" ]] || receipt="$(read_host '~/vos-fleet/experiments/shared-base-receipt.json')"
if [[ -n "$receipt" ]]; then
  python3 - "$receipt" <<'PY' 2>/dev/null || line "base+delta round-trip" "unparseable receipt"
import json, sys
d = json.loads(sys.argv[1])
ok = d.get("round_trip_ok")
print("%-26s %s  delta=%.0f MB (%.2f%%)  blocks=%d  mismatches=%d" % (
    "base+delta round-trip", "PROVEN" if ok else "FAILED",
    d.get("delta_bytes", 0)/1e6,
    100.0 * d.get("delta_bytes", 0) / max(1, d.get("logical_bytes", 1)),
    d.get("blocks_checked", 0), d.get("mismatches", -1)))
PY
elif [[ "$MINI_UP" == "0" ]]; then
  line "base+delta round-trip" "$(host_probe_status)"
else
  line "base+delta round-trip" "no receipt — run experiments shared_base.py verify"
fi

# Base+delta is servable as one disk when the stable plane knows the backend.
if grep -q "base-delta-serve" "$ROOT/tools/cloud_disk.py" 2>/dev/null; then
  line "base+delta serve" "wired (--nbd-base-delta-serve over NBD)"
else
  line "base+delta serve" "not wired"
fi
if grep -q "def upload_delta" "$ROOT/tools/cloud_disk_r2_upload.py" 2>/dev/null &&
   grep -q "def pull_delta" "$ROOT/tools/cloud_disk_r2_upload.py" 2>/dev/null; then
  line "R2 delta transfer" "wired; local signed-HTTP round-trip tested"
else
  line "R2 delta transfer" "not wired"
fi

# Historical cloud evidence and current local backup receipts. Pending parts
# are transfer progress; they are never presented as a recoverable archive.
python3 - "$ROOT" <<'PY'
import json
from pathlib import Path
import re
import sys
import time

root = Path(sys.argv[1])
def show(label, value):
    print('%-26s %s' % (label, value))

boot = root / 'tools/experiments/managed-shared-boot-receipt.json'
if boot.exists():
    try:
        d = json.loads(boot.read_text())
        verified = (d.get('managed_shared_disk_boot') == 'verified' and
                    d.get('guest_boots_verified', 0) >= 2 and
                    d.get('disk_service_restart_verified') is True and
                    d.get('restart_blocks_reseeded') == 0 and
                    d.get('source_disks_unchanged') is True and
                    d.get('local_machine_disk') == 'absent')
        if verified:
            show('managed shared boot', 'VERIFIED: %s guest boots; service restart; overlay %.1f MB (historical receipt)' %
                 (d['guest_boots_verified'], d['overlay_allocated_bytes'] / 1e6))
        else:
            show('managed shared boot', 'receipt does not prove boot + restart')
    except (OSError, ValueError, KeyError, TypeError):
        show('managed shared boot', 'invalid receipt')
else:
    show('managed shared boot', 'no real guest boot receipt')

upload = root / 'tools/experiments/r2-machine-upload-receipt.json'
if upload.exists():
    try:
        d = json.loads(upload.read_text())
        if (d.get('r2_machine_upload') != 'verified' or d.get('readback_verified') is not True or
            d.get('account_id') != 'c24a3fb4bedba669785c18c834f51921' or
            d.get('bucket') != 'virtualos-cloud-disks' or
            type(d.get('block_count')) is not int or d['block_count'] <= 0 or
            d.get('source_blocks') != d['block_count'] or
            d.get('source_raw_bytes') != d['block_count'] * 65536 or
            not re.fullmatch(r'[0-9a-f]{64}', d.get('delta_manifest_sha256', ''))):
            raise ValueError('invalid upload receipt')
        show('R2 machine upload', 'VERIFIED: %s blocks + manifest; recovery reported separately' % d['block_count'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        show('R2 machine upload', 'invalid receipt')

storage = root / 'tools/experiments/r2-machine-storage-receipt.json'
if storage.exists():
    try:
        d = json.loads(storage.read_text())
        if (d.get('r2_machine_storage') != 'verified' or
            d.get('inventory_matches_verified_manifest') is not True or
            d.get('account_id') != 'c24a3fb4bedba669785c18c834f51921' or
            d.get('bucket') != 'virtualos-cloud-disks' or
            any(type(d.get(key)) is not int for key in
                ('unique_block_objects', 'stored_objects', 'stored_bytes', 'manifest_bytes')) or
            d['unique_block_objects'] <= 0 or d['stored_objects'] != d['unique_block_objects'] + 1 or
            not 0 < d['manifest_bytes'] < d['stored_bytes'] or
            not re.fullmatch(r'[0-9a-f]{64}', d.get('manifest_sha256', ''))):
            raise ValueError('invalid storage inventory receipt')
        show('R2 delta stored', '%.1f MB; %s unique blocks + manifest (historical inventory)' %
             (d['stored_bytes'] / 1e6, d['unique_block_objects']))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        show('R2 delta stored', 'invalid inventory receipt')

machine_delta = root / 'tools/experiments/r2-machine-delta-receipt.json'
if machine_delta.exists():
    try:
        d = json.loads(machine_delta.read_text())
        if (d.get('r2_machine_delta') != 'verified' or
            d.get('account_id') != 'c24a3fb4bedba669785c18c834f51921' or
            d.get('bucket') != 'virtualos-cloud-disks' or
            d.get('all_blocks_readback_verified') is not True or
            d.get('recovered_delta_matches_source') is not True or
            type(d.get('block_count')) is not int or d['block_count'] <= 0 or
            type(d.get('raw_bytes')) is not int or d['raw_bytes'] != d['block_count'] * 65536 or
            any(not re.fullmatch(r'[0-9a-f]{64}', d.get(key, '')) for key in
                ('manifest_sha256', 'source_delta_sha256', 'required_base_sha256'))):
            raise ValueError('invalid real-machine evidence')
        show('R2 real machine delta', 'VERIFIED: %s blocks; %.3f GB; exact recovery (historical receipt)' %
             (d['block_count'], d['raw_bytes'] / 1e9))
        boot_path = root / 'tools/experiments/r2-recovered-boot-receipt.json'
        recovered = json.loads(boot_path.read_text()) if boot_path.exists() else {}
        boot_verified = (d.get('recovered_guest_boot_verified') is True and
                         d.get('recovered_guest_boots') == 2 and
                         d.get('recovered_disk_service_restart_verified') is True and
                         recovered.get('managed_shared_disk_boot') == 'verified' and
                         recovered.get('guest_boots_verified') == 2 and
                         recovered.get('restart_blocks_reseeded') == 0 and
                         recovered.get('source_disks_unchanged') is True and
                         recovered.get('initial_delta_sha256') == d['source_delta_sha256'] and
                         recovered.get('local_machine_disk') == 'absent')
        show('R2 recovered guest', 'VERIFIED: two boots + service restart (historical)' if boot_verified else
             'boot verification incomplete')
        if d.get('local_base_present') is False:
            show('R2 local test disks', 'removed; cold restore requires archived base + pinned delta')
        else:
            show('R2 recovery base', 'required shared base remains on Mini; full archive guest restore unverified')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        show('R2 real machine delta', 'invalid receipt; recovery unverified')
else:
    show('R2 real machine delta', 'no completed real-machine recovery receipt')

probe = root / 'tools/experiments/r2-delta-probe-receipt.json'
if probe.exists():
    try:
        row = json.loads(probe.read_text())
        if (row.get('account_id') != 'c24a3fb4bedba669785c18c834f51921' or
            row.get('bucket') != 'virtualos-cloud-disks' or
            row.get('r2_delta_probe') != 'passed' or row.get('readback_verified') is not True or
            row.get('scope') != 'synthetic blocks; no VM boot or archive' or
            not re.fullmatch(r'[0-9a-f]{64}', row.get('delta_manifest_sha256', ''))):
            raise ValueError('invalid cloud evidence')
        show('R2 delta evidence', 'PROVEN synthetic %s blocks; no VM delta/boot proof' % row['block_count'])
    except (ValueError, KeyError, TypeError, AttributeError):
        show('R2 delta evidence', 'invalid receipt')

state = Path.home() / '.config/virtualos'
receipt = state / 'vm-r2-receipt.json'
if receipt.exists():
    try:
        row = json.loads(receipt.read_text())
        if (row.get('bucket') != 'virtualos-vm-backups' or
            not re.fullmatch(r'vm-backups/[0-9]{8}T[0-9]{6}Z-[0-9]+/virtualOS\.bundle\.tar\.gz', row.get('key', '')) or
            not re.fullmatch(r'[0-9a-f]{64}', row.get('sha256', '')) or
            type(row.get('bytes')) is not int or row['bytes'] <= 0):
            raise ValueError('invalid archive receipt')
        show('VM archive', 'verified local receipt %.2f GB; live object not re-audited' % (row['bytes'] / 1e9))
    except (ValueError, KeyError, TypeError, AttributeError):
        show('VM archive', 'invalid receipt; recovery unverified')
else:
    show('VM archive', 'unverified; no completed download-audit receipt')

usage = state / 'r2-usage.jsonl'
if usage.exists():
    with usage.open('rb') as stream:
        stream.seek(max(0, usage.stat().st_size - 65536))
        rows = []
        for line in stream.read().splitlines():
            try:
                row = json.loads(line)
                if (row.get('bucket') == 'virtualos-vm-backups' and
                    type(row.get('pending_uploaded_bytes')) is int and
                    type(row.get('pending_parts')) is int and type(row.get('at')) is int):
                    rows.append(row)
            except (ValueError, AttributeError):
                pass
    if rows:
        row = rows[-1]
        show('VM upload last sample', '%.2f GB pending; %s parts; age=%ss' % (
            row['pending_uploaded_bytes'] / 1e9, row['pending_parts'], max(0, int(time.time()) - row['at'])))
PY

# --- P2: agency / guest input ---------------------------------------------
echo; echo "[P2] agency"
line "single-guest input" "$([[ -f "$ROOT/tools/virtualos_stream_server/inputctl.swift" ]] && echo 'inputctl present' || echo 'missing')"
# Polling removes a command from the host's queue. Reuse the read-only status
# response instead; a scoreboard must never steal a click from the real poller.
if [[ -n "$RELAY_BODY" ]]; then
  line "fleet input" "worker+host wiring present; relay status reachable; task proof separate"
elif [[ -n "${RELAY_URL:-}" ]]; then
  line "fleet input" "worker+host wiring present; relay status unavailable"
else
  line "fleet input" "wired in worker+host; no relay.env to probe"
fi
line "offline brain" "$([[ -f "$ROOT/tools/fleet/brain.py" ]] && echo 'brain.py present (click/key/type)' || echo 'missing')"

# --- P3: breadth -----------------------------------------------------------
echo; echo "[P3] breadth"
python3 "$HERE/linux_evidence.py" "$ROOT"
# Linux guest boot leaves a version in its console log: the `Linux (none) <ver>`
# banner or the shell's osrelease echo (`<ver>-0-virt`). Accept either.
LINUX_REMOTE="tr -d '\\000' < ~/vos-fleet/linux/console.log 2>/dev/null | sed 's/\\r/\\n/g' | grep -aoE 'Linux \\(none\\) [0-9][^ ]*|[0-9]+\\.[0-9]+\\.[0-9]+-[0-9a-z-]*virt' | tail -1"
linux_log="$FLEET_HOME/linux/console.log"
kern=""
if [[ -f "$linux_log" ]]; then
  kern=$(tr -d '\000' < "$linux_log" 2>/dev/null | sed 's/\r/\n/g' | grep -aoE 'Linux \(none\) [0-9][^ ]*|[0-9]+\.[0-9]+\.[0-9]+-[0-9a-z-]*virt' | tail -1)
  linux_src=""
else
  kern="$(mini_run "$LINUX_REMOTE" || true)"
  linux_src=" [mini]"
fi
if [[ -n "$kern" ]]; then
  line "linux guest" "${kern}${linux_src}"
elif [[ "$MINI_UP" == "0" ]]; then
  line "linux guest" "$(host_probe_status)"
else
  line "linux guest" "not booted this session"
fi
hyp="$(mini_run "tr -d '\\000' < ~/vos-fleet/linux/console.log 2>/dev/null | grep -am1 'HYP mode not available'" || true)"
[[ -n "$hyp" ]] && line "nested (VM-in-VM)" "BLOCKED: guest has no EL2/HYP"
line "windows" "no path"

# --- P6: discipline --------------------------------------------------------
echo; echo "[P6] discipline"
line "manifesto" "$(grep -c '^- \*\*' "$ROOT/MANIFESTO.md" 2>/dev/null) earned entries"
# Count assertions (pass "..." call sites), not groups, so the scoreboard shows
# the real check count and never drifts from the script.
self_test="$FLEET_HOME/tools/fleet/self_test.sh"
[[ -x "$self_test" ]] || self_test="$ROOT/tools/fleet/self_test.sh"
if [[ -x "$self_test" ]]; then
  checks="$(grep -cE 'pass "' "$self_test" 2>/dev/null || echo '?')"
  line "fleet self-test" "available (${checks} checks)"
elif [[ "$MINI_UP" == "1" ]]; then
  st="$(mini_run '[[ -x ~/vos-fleet/tools/fleet/self_test.sh ]] && echo "mini" || echo n/a' || echo n/a)"
  line "fleet self-test" "$st"
else
  line "fleet self-test" "$(host_probe_status)"
fi
relay_test="$ROOT/tools/cloudflare/worker/test_relay_local.sh"
[[ -f "$relay_test" ]] && line "relay test" "offline (wrangler dev --local)" || line "relay test" "missing"

sep
echo "verdict: a blocked hard truth is a result. drive the next number down."
