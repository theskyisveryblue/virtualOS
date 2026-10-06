#!/usr/bin/env bash
# Read-only status for guest computer-use practice on the Mac mini.
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo "Usage: ./tools/computer-status.sh" >&2
  exit 2
fi

trace_dir="${VOS_CUA_TRACE_DIR:-$HOME/Library/Application Support/virtualOS/computer-use}"
free_kb="$(df -Pk /System/Volumes/Data | awk 'NR == 2 {print $4}')"
printf 'disk_free_kib=%s\n' "$free_kb"
if pgrep -fl '[v]osctl computer-night' 2>/dev/null; then
  :
else
  echo 'runner=stopped'
fi
quality_pid="$(pgrep -f '[c]omputer-night.sh --quality' | head -1 || true)"
if [[ -n "$quality_pid" ]]; then
  printf 'quality_runner=running pid=%s\n' "$quality_pid"
else
  echo 'quality_runner=stopped'
fi
if health_json="$(curl -fsS --max-time 3 http://127.0.0.1:8845/vm/health 2>/dev/null)" \
    && [[ -n "$health_json" ]]; then
  printf '%s' "$health_json" | python3 -c 'import json,sys
try: h=json.load(sys.stdin)
except (ValueError, TypeError): h={}
print("guest_ready="+str(bool(isinstance(h,dict) and h.get("ready") and h.get("state")=="running")).lower())'
else
  echo 'guest_ready=false'
fi
if curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo 'ollama_ready=true'
else
  echo 'ollama_ready=false'
fi
python3 - "$trace_dir" <<'PY'
import collections
import json
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
try:
    receipt = json.loads((root / 'r2-receipt.json').read_text())
    sha = receipt.get('sha256', '')
    print('r2_receipt_sha256=' + sha if re.fullmatch(r'[0-9a-f]{64}', sha)
          else 'r2_receipt_sha256=invalid')
except (OSError, ValueError, TypeError):
    print('r2_receipt_sha256=missing')
rows = root / 'steps.jsonl'
if not rows.is_file():
    print('traces=0')
else:
    totals = collections.Counter()
    with rows.open(encoding='utf-8') as stream:
        for line in stream:
            item = json.loads(line)
            totals['traces'] += 1
            if item.get('executed') and item.get('verified') is True:
                totals['verified_clicks'] += 1
            if item.get('executed') and item.get('verified') is False:
                totals['verified_misses'] += 1
            if item.get('action') == 'abstain':
                totals['abstentions'] += 1
    for key in ('traces', 'verified_clicks', 'verified_misses', 'abstentions'):
        print(f'{key}={totals[key]}')
PY
