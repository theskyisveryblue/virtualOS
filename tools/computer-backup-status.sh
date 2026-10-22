#!/usr/bin/env bash
# Read-only status for the detached VM archive and its local recovery clone.
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo 'Usage: ./tools/computer-backup-status.sh' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
status_failed=0
sessions="$(screen -ls 2>&1 || true)"
if grep -q '[.]virtualos-vm-backup' <<< "$sessions"; then
  echo 'backup=running_detached'
elif backup_pid="$(pgrep -f '[c]omputer-r2-sync.sh (vm-backup|vm-resume)$' 2>/dev/null | head -1)" && [[ -n "$backup_pid" ]]; then
  printf 'backup=running pid=%s\n' "$backup_pid"
else
  echo 'backup=stopped'
fi
if grep -q '[.]virtualos-vm-monitor' <<< "$sessions"; then
  echo 'backup_monitor=running_detached'
elif monitor_pid="$(pgrep -f '[c]omputer-r2-sync.sh vm-monitor$' 2>/dev/null | head -1)" && [[ -n "$monitor_pid" ]]; then
  printf 'backup_monitor=running pid=%s\n' "$monitor_pid"
else
  echo 'backup_monitor=stopped'
fi

receipt="$HOME/.config/virtualos/vm-r2-receipt.json"
if [[ -r "$receipt" ]]; then
  python3 - "$receipt" <<'PY'
import json
from pathlib import Path
import re
import sys

try:
    row = json.loads(Path(sys.argv[1]).read_text())
    valid = (isinstance(row, dict) and row.get('bucket') == 'virtualos-vm-backups'
             and isinstance(row.get('sha256'), str)
             and re.fullmatch(r'[0-9a-f]{64}', row['sha256'])
             and type(row.get('bytes')) is int and row['bytes'] > 0)
    if not valid:
        raise ValueError('invalid receipt')
    print(f"backup_receipt=present bytes={row['bytes']} sha256={row['sha256']}")
except (OSError, ValueError, KeyError, TypeError):
    print('backup_receipt=invalid')
PY
else
  echo 'backup_receipt=absent'
fi

pending_receipt="$HOME/.config/virtualos/vm-r2-pending.json"
if [[ -r "$pending_receipt" ]]; then
  python3 - "$pending_receipt" <<'PY'
import json
from pathlib import Path
import re
import sys
try:
    row = json.loads(Path(sys.argv[1]).read_text())
    if (not isinstance(row, dict) or row.get('bucket') != 'virtualos-vm-backups' or
        not isinstance(row.get('sha256'), str) or
        not re.fullmatch(r'[0-9a-f]{64}', row['sha256']) or
        type(row.get('bytes')) is not int or row['bytes'] <= 0):
        raise ValueError('invalid candidate')
    print(f"backup_pending_receipt=unverified bytes={row['bytes']}")
except (OSError, ValueError, TypeError):
    print('backup_pending_receipt=invalid')
PY
else
  echo 'backup_pending_receipt=absent'
fi

if "$repo_dir/tools/computer-r2-sync.sh" vm-usage; then
  "$repo_dir/tools/computer-r2-sync.sh" vm-estimate || true
else
  echo 'backup_r2=unavailable'
  status_failed=1
fi
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "${VOS_VM_SSH_HOST:-mac-mini-ai}" \
  'cd ~/vos-fleet && ./tools/computer-r2.sh vm-clone-status && ./tools/vosctl computer doctor'; then
  echo 'backup_mini=unavailable'
  status_failed=1
fi
exit "$status_failed"
