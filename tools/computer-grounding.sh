#!/usr/bin/env bash
# Read-only check of saved Finder model points against OCR label hit regions.
set -euo pipefail

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --rerun-model && "$1" != --missing-target && "$1" != --check-guard && "$1" != --retry-false-done && "$1" != --inventory && "$1" != --new-targets && "$1" != --benchmark ) ]]; then
  echo 'Usage: ./tools/computer-grounding.sh [--rerun-model|--missing-target|--check-guard|--retry-false-done|--inventory|--new-targets|--benchmark]' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
root="${VOS_CUA_TRACE_DIR:-$HOME/Library/Application Support/virtualOS/computer-use/journeys}"
python3 - "$repo_dir/tools" "$root" "${1:-}" <<'PY'
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen

sys.path.insert(0, sys.argv[1])
from computer_use import (Frame, _ocr_entries, _ollama_action,
                          _confirmed_finder_sidebar_click, _finder_label_center,
                          _finder_model_action, _finder_sidebar_label)

root = Path(sys.argv[2])
rerun = sys.argv[3] == '--rerun-model'
missing_target = sys.argv[3] == '--missing-target'
check_guard = sys.argv[3] == '--check-guard'
retry_false_done = sys.argv[3] == '--retry-false-done'
inventory = sys.argv[3] == '--inventory'
new_targets = sys.argv[3] == '--new-targets'
benchmark = sys.argv[3] == '--benchmark'
model = os.getenv('VOS_OLLAMA_MODEL', 'qwen3.5:0.8b')
reviews = {item['id']: item for item in
           (json.loads(line) for line in (root / 'reviews.jsonl').read_text().splitlines())}
rows = [json.loads(line) for line in (root / 'steps.jsonl').read_text().splitlines()]
if benchmark:
    revision = os.getenv('VOS_CUA_BENCH_REV', '1')
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,32}', revision):
        raise SystemExit('Invalid benchmark revision')
    selected = {}
    for row in rows:
        review = reviews.get(row.get('id'), {})
        target = row.get('target_view')
        if (target in {'Applications', 'Downloads', 'Documents', 'Desktop', 'Recents'}
                and review.get('decision') == 'accept' and row.get('verified') is True
                and review.get('frame_sha256') == row.get('frame_sha256')
                and review.get('after_sha256') == row.get('after_sha256')):
            selected.setdefault(target, row)
    if len(selected) < 2:
        raise SystemExit('benchmark=refused: fewer than two reviewed Finder targets')
    chosen = [(name, selected[name]) for name in sorted(selected)]
    key_data = [(name, row['frame_sha256'], row['after_sha256']) for name, row in chosen]
    endpoint = os.getenv('VOS_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
    if endpoint not in {'http://127.0.0.1:11434', 'http://localhost:11434'}:
        raise SystemExit('benchmark=refused: model endpoint must be local')
    with urlopen(endpoint + '/api/tags', timeout=5) as response:
        tags = json.load(response).get('models', [])
    digests = [item.get('digest') for item in tags if item.get('name') == model]
    if len(digests) != 1 or not re.fullmatch(r'[0-9a-f]{64}', digests[0] or ''):
        raise SystemExit('benchmark=refused: model digest unavailable')
    key = hashlib.sha256(json.dumps([model, digests[0], revision, key_data], sort_keys=True).encode()).hexdigest()
    summary_path = root / 'benchmark-summary.json'
    if summary_path.is_file():
        previous = json.loads(summary_path.read_text())
        if previous.get('key') == key and previous.get('complete') is True:
            print(json.dumps({'benchmark': 'cached', 'targets': previous['targets'],
                              'scores': previous['scores'], 'guest_actions': 0,
                              'images_uploaded': 0}, sort_keys=True))
            raise SystemExit(0)

    def load_reviewed_frame(row, path_key, hash_key):
        if shutil.disk_usage(root).free < 3 * 1024 ** 3:
            raise SystemExit('benchmark=stopped: disk below 3 GiB')
        relative = row[path_key]
        if not re.fullmatch(r'frames/[0-9a-f]{32}(?:-after)?\.jpg', relative):
            raise ValueError('Invalid reviewed frame path')
        image = root / relative
        blob = image.read_bytes()
        if hashlib.sha256(blob).hexdigest() != row[hash_key]:
            raise ValueError('Reviewed frame hash mismatch')
        size = subprocess.run(['sips', '-g', 'pixelWidth', '-g', 'pixelHeight', str(image)],
                              capture_output=True, text=True, check=True).stdout
        width = int(re.search(r'pixelWidth: (\d+)', size).group(1))
        height = int(re.search(r'pixelHeight: (\d+)', size).group(1))
        return Frame(blob, width, height, width, height)

    scores = {'needed_click': 0, 'already_done': 0, 'missing_abstain': 0}
    for target, row in chosen:
        before = load_reviewed_frame(row, 'frame', 'frame_sha256')
        click = _ollama_action(before, f'Click {target} in the Finder sidebar', model)
        scores['needed_click'] += int(_confirmed_finder_sidebar_click(before, click, target) is not None)
        if shutil.disk_usage(root).free < 3 * 1024 ** 3:
            raise SystemExit('benchmark=stopped: disk below 3 GiB')
        absent = _ollama_action(before, 'Click the purple Martian icon in the Finder sidebar', model)
        scores['missing_abstain'] += int(absent.get('type') == 'abstain')
        after = load_reviewed_frame(row, 'after_frame', 'after_sha256')
        done = _ollama_action(after, f'Click {target} in the Finder sidebar', model)
        scores['already_done'] += int(done.get('type') == 'done')
    summary = {'key': key, 'complete': True, 'targets': len(chosen), 'scores': scores,
               'cases_per_category': len(chosen), 'guest_actions': 0, 'images_uploaded': 0}
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=root,
                                     prefix='.benchmark-', suffix='.json', delete=False) as stream:
        os.chmod(stream.name, 0o600)
        json.dump(summary, stream, sort_keys=True)
        stream.write('\n')
        temporary = stream.name
    os.replace(temporary, summary_path)
    print(json.dumps({'benchmark': 'complete', **summary}, sort_keys=True))
    raise SystemExit(0)

results = []
for row in rows:
    if (inventory or new_targets) and row is not rows[-1]:
        continue
    if not retry_false_done and reviews.get(row['id'], {}).get('decision') != 'accept':
        if not inventory and not new_targets:
            continue
    if retry_false_done and not (row.get('model_action') == 'done'
                                 and row.get('corrected_by_ocr') is True):
        continue
    image = root / row['frame']
    if not image.is_file():
        raise ValueError('Accepted Finder frame is missing')
    # Only OCR is used; the private image bytes never leave this host.
    from subprocess import run
    size = run(['sips', '-g', 'pixelWidth', '-g', 'pixelHeight', str(image)],
               capture_output=True, text=True, check=True).stdout
    width = int(re.search(r'pixelWidth: (\d+)', size).group(1))
    height = int(re.search(r'pixelHeight: (\d+)', size).group(1))
    frame = Frame(image.read_bytes(), width, height, width, height)
    target = row['target_view']
    if new_targets:
        for name in ('Desktop', 'Recents'):
            action, first_type, retry_reason = _finder_model_action(frame, name, model)
            results.append({'target': name, 'model_action': action.get('type'),
                            'first_model_action': first_type, 'retry_reason': retry_reason,
                            'guarded_point': _confirmed_finder_sidebar_click(frame, action, name) is not None})
        continue
    if inventory:
        entries = _ocr_entries(frame) or []
        counts = {}
        for name in ('Applications', 'Downloads', 'Documents', 'Desktop', 'Recents'):
            matches = [entry for entry in entries
                       if re.fullmatch(r'[^A-Za-z]{0,3}' + re.escape(name),
                                       entry.get('text', '').strip())
                       and 0.03 < entry.get('x', -1) < 0.22
                       and 0.20 < entry.get('y', -1) < 0.85]
            if len(matches) == 1:
                entry = matches[0]
                action = {'type': 'click',
                          'x': round((entry['x'] + entry['width'] / 2) * width),
                          'y': round((1 - entry['y'] - entry['height'] / 2) * height)}
                counts[name] = _confirmed_finder_sidebar_click(frame, action, name) is not None
            else:
                counts[name] = False
        results.append({'guarded_sidebar_targets': [name for name, valid in counts.items() if valid]})
        continue
    if retry_false_done:
        task = row['task'] + f'. The Finder title does not show {target}; click its visible sidebar label or abstain.'
        action = _ollama_action(frame, task, model)
        results.append({'target': target, 'retry_action': action.get('type'),
                        'retry_hit_region': _confirmed_finder_sidebar_click(frame, action, target) is not None})
        continue
    if check_guard:
        point = row.get('proposed_coordinates') or {}
        action = {'type': 'click', **point}
        label = _finder_sidebar_label(frame, target)
        if label is None:
            raise ValueError('Reviewed target label is not uniquely visible')
        cx, cy = _finder_label_center(frame, label)
        center_accepted = _confirmed_finder_sidebar_click(
            frame, {'type': 'click', 'x': cx, 'y': cy}, target) is not None
        saved_point_accepted = _confirmed_finder_sidebar_click(frame, action, target) is not None
        rejected_absent = _confirmed_finder_sidebar_click(frame, action, 'Purple Martian') is None
        results.append({'target': target, 'label_center_accepted': center_accepted,
                        'model_point_accepted': saved_point_accepted,
                        'model_was_executed': row.get('action_source') == 'model',
                        'absent_target_rejected': rejected_absent})
        continue
    if missing_target:
        action = _ollama_action(frame, 'Click the purple Martian icon in the Finder sidebar', model)
        results.append({'target': target, 'missing_target_action': action.get('type'),
                        'abstained': action.get('type') == 'abstain'})
        continue
    entries = _ocr_entries(frame)
    labels = [entry for entry in (entries or [])
              if re.fullmatch(r'[^A-Za-z]{0,3}' + re.escape(target),
                              entry.get('text', '').strip())
              and 0.03 < entry.get('x', -1) < 0.22
              and 0.20 < entry.get('y', -1) < 0.85]
    if len(labels) != 1:
        raise ValueError(f'Accepted Finder target {target} is not uniquely visible: '
                         f'ocr_available={entries is not None} matching_labels={len(labels)}')
    label = labels[0]
    if rerun:
        action = _ollama_action(frame, row['task'], model)
        point = {'x': action.get('x'), 'y': action.get('y')} if action.get('type') == 'click' else {}
    else:
        point = row.get('proposed_coordinates') or {}
    x, y = point.get('x'), point.get('y')
    valid = isinstance(x, (int, float)) and isinstance(y, (int, float)) and math.isfinite(x) and math.isfinite(y)
    left = label['x'] * width - 30
    right = (label['x'] + label['width']) * width + 30
    top = (1 - label['y'] - label['height']) * height - 5
    bottom = (1 - label['y']) * height + 5
    in_hit_region = bool(valid and left <= x <= right and top <= y <= bottom)
    center_x = (label['x'] + label['width'] / 2) * width
    center_y = (1 - label['y'] - label['height'] / 2) * height
    results.append({'target': target, 'model_point_in_hit_region': in_hit_region,
                    'proposal_source': 'fresh_model' if rerun else 'recorded',
                    'center_offset_px': [round(x - center_x), round(y - center_y)] if valid else None,
                    'executed_action_source': row.get('action_source', 'ocr')})

if new_targets:
    print(json.dumps({'targets_checked': len(results), 'guarded_model_points': sum(item['guarded_point'] for item in results),
                      'details': results}, sort_keys=True))
elif inventory:
    print(json.dumps({'frames_checked': len(results), 'details': results}, sort_keys=True))
elif retry_false_done:
    print(json.dumps({'false_done_examples': len(results),
                      'retry_hits': sum(item['retry_hit_region'] for item in results),
                      'details': results}, sort_keys=True))
elif check_guard:
    passed = bool(results) and all(item['label_center_accepted'] and item['absent_target_rejected']
                                   and (not item['model_was_executed'] or item['model_point_accepted'])
                                   for item in results)
    print(json.dumps({'reviewed_finder_examples': len(results), 'guard_passed': passed,
                      'details': results}, sort_keys=True))
    raise SystemExit(0 if passed else 1)
elif missing_target:
    print(json.dumps({'reviewed_finder_examples': len(results),
                      'missing_target_abstentions': sum(item['abstained'] for item in results),
                      'details': results}, sort_keys=True))
else:
    print(json.dumps({'reviewed_finder_examples': len(results),
                      'model_points_in_hit_region': sum(item['model_point_in_hit_region'] for item in results),
                      'details': results}, sort_keys=True))
PY
