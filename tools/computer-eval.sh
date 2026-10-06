#!/usr/bin/env bash
# Read-only outcome report for private guest computer-use traces.
set -euo pipefail

if [[ $# -gt 1 || ( $# -eq 1 && ! "$1" =~ ^[1-9][0-9]*$ ) ]]; then
  echo 'Usage: ./tools/computer-eval.sh [recent-minutes]' >&2
  exit 2
fi

minutes="${1:-60}"
root="${VOS_CUA_TRACE_DIR:-$HOME/Library/Application Support/virtualOS/computer-use}"
python3 - "$root" "$minutes" <<'PY'
import collections
import json
from pathlib import Path
import sys
import time

root = Path(sys.argv[1])
cutoff = time.time() - int(sys.argv[2]) * 60

def read_rows(path):
    if not path.is_file():
        return []
    with path.open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]

def count(rows):
    totals = collections.Counter()
    by_target = {}
    for row in rows:
        target = row.get('target_view') or row.get('target_app') or 'other'
        group = by_target.setdefault(target, collections.Counter())
        for bucket in (totals, group):
            bucket['traces'] += 1
            if row.get('action') == 'done' and row.get('verified') is True:
                bucket['already_complete'] += 1
            elif row.get('action') == 'done':
                bucket['false_done'] += 1
            elif row.get('action') == 'abstain':
                bucket['abstained'] += 1
            elif row.get('executed') is True and row.get('verified') is True:
                bucket['verified_actions'] += 1
            elif row.get('executed') is True and row.get('verified') is False:
                bucket['verified_misses'] += 1
            elif row.get('executed') is True:
                bucket['unverified_actions'] += 1
            if row.get('action_source') not in {'offline_policy_label_guarded',
                                                'offline_locator_tooltip_guarded'}:
                if row.get('proposal_near_target') is True:
                    bucket['model_proposal_near'] += 1
                elif row.get('proposal_near_target') is False:
                    bucket['model_proposal_far_or_empty'] += 1
            if row.get('action_source') == 'model':
                bucket['model_clicks_executed'] += 1
            if row.get('action_source') == 'ocr':
                bucket['ocr_clicks_executed'] += 1
            if row.get('action_source') in {'offline_policy_label_guarded',
                                            'offline_locator_tooltip_guarded'}:
                bucket['offline_guarded_clicks'] += 1
            if row.get('model_false_done') is True or (
                    row.get('model_action') == 'done' and row.get('corrected_by_ocr') is True):
                bucket['model_false_done_corrected'] += 1
    return {'totals': dict(totals), 'by_target': {k: dict(v) for k, v in sorted(by_target.items())}}

all_rows = read_rows(root / 'steps.jsonl') + read_rows(root / 'journeys/steps.jsonl')
recent_rows = [row for row in all_rows if row.get('ts', 0) >= cutoff]
print(json.dumps({'recent_minutes': int(sys.argv[2]), 'recent': count(recent_rows),
                  'all': count(all_rows)}, sort_keys=True))
PY
