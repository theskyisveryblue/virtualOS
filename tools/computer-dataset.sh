#!/usr/bin/env bash
# Export only manually accepted, hash-verified guest click examples locally.
set -euo pipefail

if [[ "${1:-}" == --playbook-self-test ]]; then
  [[ $# -eq 1 ]] || { echo 'playbook_self_test=refused: no extra arguments' >&2; exit 2; }
  python3 -B - "$(dirname "$0")" <<'PY'
import copy, json, sys
sys.path.insert(0, sys.argv[1])
from intent_plan import PlanError, _unique_pairs, playbook_object_key, validate_playbook

base = {
    "schema": "virtualos.playbook/v1", "playbook_id": "finder.desktop", "revision": 1,
    "scope": {"kind": "app", "id": "com.apple.finder", "version": "15.0", "profile_id": "finder.profile.v1"},
    "goal": "navigate.desktop", "steps": [{"action": {"verb": "click", "target": "sidebar.desktop"},
        "expect": "finder.desktop", "verify": {"kind": "ax", "state": "finder.desktop"}}],
    "rights": {"ownership": "unknown", "training": "unknown", "consent_id": None},
    "privacy": {"mode": "metadata-only", "cloud": "private-r2"},
    "split": {"role": "unassigned", "group_id": "finder.desktop", "policy": "goal-and-state-v1", "review_id": None},
    "source": {"kind": "declared-fixture", "sha256": "a" * 64, "evidence": "declared"},
}
assert validate_playbook(base) is base
mixed_case = copy.deepcopy(base)
mixed_case["scope"]["id"] = "com.apple.Terminal"
assert validate_playbook(mixed_case) is mixed_case
action_id = copy.deepcopy(base)
action_id["steps"][0]["action"]["id"] = "finder_sidebar_click"
assert validate_playbook(action_id) is action_id
assert playbook_object_key(base) == playbook_object_key(copy.deepcopy(base))
changed = copy.deepcopy(base)
changed["source"]["sha256"] = "b" * 64
assert playbook_object_key(changed) != playbook_object_key(base)
bad = []
for field, value in (("kind", []), ("kind", {})):
    item = copy.deepcopy(base)
    item["scope"][field] = value
    bad.append(item)
for role in ("train", "validation", "holdout"):
    item = copy.deepcopy(base)
    item["split"].update(role=role, review_id="review.one")
    bad.append(item)
for item in bad:
    try:
        validate_playbook(item)
    except PlanError:
        pass
    else:
        raise AssertionError("invalid playbook accepted")
try:
    json.loads('{"rights":{},"rights":{}}', object_pairs_hook=_unique_pairs)
except PlanError:
    pass
else:
    raise AssertionError("duplicate JSON field accepted")
print("playbook_self_test=passed cases=11")
PY
  exit 0
fi

if [[ $# -gt 2 || ( $# -eq 1 && "$1" != --readiness && "$1" != --episode-candidates && "$1" != --episode-audit && "$1" != --episode-leakage ) ||
      ( $# -eq 2 && "$1" != --record-style && "$1" != --playbook-validate && "$1" != --playbook-export ) ]]; then
  echo 'Usage: ./tools/computer-dataset.sh [--readiness|--episode-candidates|--episode-audit|--episode-leakage|--record-style SIGNALS|--playbook-validate FILE|--playbook-export FILE]' >&2
  exit 2
fi

if [[ "${1:-}" == --playbook-validate || "${1:-}" == --playbook-export ]]; then
  python3 -B "$(dirname "$0")/intent_plan.py" "${1#--}" "$2"
  exit 0
fi

root="${VOS_CUA_TRACE_DIR:-$HOME/Library/Application Support/virtualOS/computer-use}"
if [[ "${1:-}" == --episode-leakage ]]; then
  "$0" --episode-audit >/dev/null
  python3 - "$root" <<'PY'
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
rows = [json.loads(line) for line in (root / 'episode-candidates.jsonl').read_text(encoding='utf-8').splitlines()]
parent = list(range(len(rows)))

def find(index):
    while parent[index] != index:
        parent[index] = parent[parent[index]]
        index = parent[index]
    return index

def merge(left, right):
    parent[find(left)] = find(right)

by_goal = defaultdict(list)
by_start = defaultdict(list)
by_image = defaultdict(list)
for index, row in enumerate(rows):
    by_goal[(row['app_id'], row['surface_id'], row['goal_id'])].append(index)
    if row['before_sha256']:
        by_start[row['before_sha256']].append(index)
    for name in ('before_sha256', 'after_sha256'):
        if row[name]:
            by_image[row[name]].append(index)
for groups in (by_goal, by_image):
    for indices in groups.values():
        for index in indices[1:]:
            merge(indices[0], index)
components = Counter(find(index) for index in range(len(rows)))
accepted = [row for row in rows if row['review'] == 'accepted_success']
candidate_ids = {row['episode_id'] for row in rows}
reviewed_by_source = {}
for source, dataset in (('dock', root / 'dataset.jsonl'),
                        ('journeys', root / 'journeys/dataset.jsonl')):
    if dataset.is_file() and dataset.stat().st_size > 8 * 1024 * 1024:
        raise SystemExit('episode_leakage=failed: reviewed dataset oversized')
    reviewed_by_source[source] = ([json.loads(line) for line in dataset.read_text(encoding='utf-8').splitlines()]
                                  if dataset.is_file() else [])
reviewed = reviewed_by_source['dock'] + reviewed_by_source['journeys']
reviewed_hashes = [row.get('image_sha256') for row in reviewed if row.get('image_sha256')]
start_repeats = [len(indices) for indices in by_start.values() if len(indices) > 1]
cross_goal_starts = sum(len({rows[index]['goal_id'] for index in indices}) > 1
                        for indices in by_start.values())
report = {
    'episode_leakage': 'measured',
    'episode_count': len(rows),
    'accepted_candidate_count': len(accepted),
    'reviewed_example_count': len(reviewed),
    'reviewed_dock_example_count': len(reviewed_by_source['dock']),
    'reviewed_journey_example_count': len(reviewed_by_source['journeys']),
    'reviewed_examples_with_candidate': sum(row.get('id') in candidate_ids for row in reviewed),
    'reviewed_duplicate_start_images': len(reviewed_hashes) - len(set(reviewed_hashes)),
    'distinct_goal_groups': len(by_goal),
    'exact_start_reuse_groups': len(start_repeats),
    'exact_start_reuse_extra_episodes': sum(size - 1 for size in start_repeats),
    'exact_start_shared_across_goals': cross_goal_starts,
    'proposed_grouping': 'connected_components_of_same_app_surface_goal_or_shared_evidence_hash',
    'proposed_group_count': len(components),
    'largest_proposed_group': max(components.values()),
    'profile_version_missing': sum(row['app_profile_id'] is None for row in rows),
    'training_rights_unknown': sum(row['training_rights'] == 'unknown' for row in rows),
    'split_assigned': sum(row['split'] != 'unassigned' for row in rows),
    'split_written': False,
    'images_or_task_text_output': False,
}
print(json.dumps(report, sort_keys=True))
PY
  exit 0
fi
if [[ "${1:-}" == --episode-audit ]]; then
  python3 - "$root" <<'PY'
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys

path = Path(sys.argv[1]).resolve() / 'episode-candidates.jsonl'
if not path.is_file() or path.stat().st_size > 1024 * 1024:
    raise SystemExit('episode_audit=failed: missing or oversized candidate file')
if path.stat().st_mode & 0o077:
    raise SystemExit('episode_audit=failed: candidate file is not private')
keys = {'schema', 'episode_id', 'app_id', 'app_profile_id', 'surface_id',
        'task_family', 'goal_id', 'outcome', 'verifier', 'review',
        'training_rights', 'split', 'before_sha256', 'after_sha256'}
apps = {'com.apple.finder', 'com.apple.Terminal', 'com.apple.Safari', 'com.google.Chrome'}
view_goals = {'finder.applications', 'finder.downloads', 'finder.documents',
              'finder.desktop', 'finder.recents'}
source_path = path.parent / 'steps.jsonl'
if not source_path.is_file() or source_path.stat().st_size > 8 * 1024 * 1024:
    raise SystemExit('episode_audit=failed: source trace journal missing or oversized')
review_path = path.parent / 'reviews.jsonl'
if review_path.is_file() and review_path.stat().st_size > 1024 * 1024:
    raise SystemExit('episode_audit=failed: review journal oversized')
reviews = {}
if review_path.is_file():
    for line in review_path.read_text(encoding='utf-8').splitlines():
        item = json.loads(line)
        identity = item.get('id')
        if isinstance(identity, str) and re.fullmatch(r'[0-9a-f]{32}', identity):
            if identity in reviews:
                raise SystemExit('episode_audit=failed: duplicate review ID')
            reviews[identity] = item
source_events = {}
for line in source_path.read_text(encoding='utf-8').splitlines():
    event = json.loads(line)
    identity = event.get('id')
    if isinstance(identity, str) and re.fullmatch(r'[0-9a-f]{32}', identity):
        if identity in source_events:
            raise SystemExit('episode_audit=failed: duplicate source trace ID')
        source_events[identity] = event
dock_tasks = {
    'com.apple.finder': ('Finder', 'Click the blue smiling Finder icon in the guest Dock'),
    'com.apple.Terminal': ('Terminal', 'Click the black Terminal icon in the guest Dock'),
    'com.apple.Safari': ('Safari', 'Click the blue Safari compass icon in the guest Dock'),
    'com.google.Chrome': ('Google Chrome', 'Click the multicolor Google Chrome icon in the guest Dock'),
}
counts = Counter()
seen = set()
for line in path.read_text(encoding='utf-8').splitlines():
    row = json.loads(line)
    identity = row.get('episode_id')
    if (set(row) != keys or row.get('schema') != 'computer-use.episode-candidate/v1'
            or not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity)
            or identity in seen or row.get('app_id') not in apps
            or row.get('app_profile_id') is not None
            or row.get('surface_id') not in {'finder.sidebar', 'macos.dock'}
            or row.get('task_family') not in {'navigate_location', 'open_app'}
            or not isinstance(row.get('goal_id'), str)
            or (row['goal_id'] not in view_goals if row.get('surface_id') == 'finder.sidebar'
                else row['goal_id'] != 'open.' + row['app_id'].lower().replace('.', '_'))
            or (row.get('app_id') != 'com.apple.finder' if row.get('surface_id') == 'finder.sidebar' else False)
            or (row.get('task_family') != 'navigate_location' if row.get('surface_id') == 'finder.sidebar'
                else row.get('task_family') != 'open_app')
            or row.get('outcome') not in {'verified_success', 'verified_miss', 'already_complete', 'abstain', 'unverified'}
            or row.get('verifier') not in {None, 'guest_ocr'}
            or row.get('review') not in {'accepted_success', 'rejected', 'pending'}
            or (row.get('review') == 'accepted_success' and row.get('outcome') != 'verified_success')
            or (row.get('outcome') in {'verified_success', 'verified_miss', 'already_complete'}
                and row.get('verifier') != 'guest_ocr')
            or row.get('training_rights') != 'unknown' or row.get('split') != 'unassigned'):
        raise SystemExit('episode_audit=failed: invalid or unexpected metadata')
    for name in ('before_sha256', 'after_sha256'):
        value = row[name]
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)):
            raise SystemExit('episode_audit=failed: invalid evidence hash')
    source = source_events.get(identity)
    view = source.get('target_view') if source else None
    expected_task = (f'Click {view} in the Finder sidebar' if view else
                     dock_tasks[row['app_id']][1])
    expected_app = 'Finder' if view else dock_tasks[row['app_id']][0]
    if (source is None or source.get('task') != expected_task
            or source.get('target_app') != expected_app
            or (view is not None and row['goal_id'] != 'finder.' + view.lower())
            or source.get('frame_sha256') != row['before_sha256']
            or source.get('after_sha256') != row['after_sha256']
            or (row['outcome'] == 'verified_success') !=
               (source.get('executed') is True and source.get('verified') is True)
            or (row['outcome'] == 'verified_miss') !=
               (source.get('executed') is True and source.get('verified') is False)
            or (row['outcome'] == 'already_complete') !=
               (source.get('executed') is not True and source.get('action') == 'done'
                and source.get('verified') is True)
            or (row['outcome'] == 'abstain') != (source.get('action') == 'abstain')
            or (row['verifier'] == 'guest_ocr') != (type(source.get('verified')) is bool)):
        raise SystemExit('episode_audit=failed: candidate differs from source trace')
    review = reviews.get(identity, {})
    accepted = (review.get('decision') == 'accept'
                and row['outcome'] == 'verified_success'
                and isinstance(row['before_sha256'], str)
                and isinstance(row['after_sha256'], str)
                and review.get('frame_sha256') == row['before_sha256']
                and review.get('after_sha256') == row['after_sha256'])
    expected_review = ('accepted_success' if accepted else
                       'rejected' if review.get('decision') == 'reject' else 'pending')
    if row['review'] != expected_review:
        raise SystemExit('episode_audit=failed: candidate review differs from review journal')
    seen.add(identity)
    counts[row['outcome']] += 1
if not seen:
    raise SystemExit('episode_audit=failed: empty candidate file')
print(json.dumps({'episode_audit': 'passed', 'episodes': len(seen),
                  'by_outcome': dict(sorted(counts.items())),
                  'metadata_only': True, 'style_included': False,
                  'ready_for_training': False}, sort_keys=True))
PY
  exit 0
fi
if [[ "${1:-}" == --episode-candidates ]]; then
  python3 - "$root" <<'PY'
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys
import tempfile

root = Path(sys.argv[1]).resolve()
journal = root / 'steps.jsonl'
if not journal.is_file():
    raise SystemExit('episode_candidates=unavailable: trace journal missing')
if journal.stat().st_size > 8 * 1024 * 1024:
    raise SystemExit('episode_candidates=refused: trace journal exceeds 8 MiB')
apps = {'Finder': 'com.apple.finder', 'Terminal': 'com.apple.Terminal',
        'Safari': 'com.apple.Safari', 'Google Chrome': 'com.google.Chrome'}
views = {'Applications', 'Downloads', 'Documents', 'Desktop', 'Recents'}
dock_tasks = {
    'Finder': 'Click the blue smiling Finder icon in the guest Dock',
    'Terminal': 'Click the black Terminal icon in the guest Dock',
    'Safari': 'Click the blue Safari compass icon in the guest Dock',
    'Google Chrome': 'Click the multicolor Google Chrome icon in the guest Dock',
}
reviews = {}
review_file = root / 'reviews.jsonl'
if review_file.is_file():
    if review_file.stat().st_size > 1024 * 1024:
        raise SystemExit('episode_candidates=refused: review journal exceeds 1 MiB')
    for line in review_file.read_text(encoding='utf-8').splitlines():
        item = json.loads(line)
        if re.fullmatch(r'[0-9a-f]{32}', str(item.get('id', ''))):
            if item['id'] in reviews:
                raise SystemExit('episode_candidates=refused: duplicate review ID')
            reviews[item['id']] = item
rows = []
counts = Counter()
seen = set()
for line in journal.read_text(encoding='utf-8').splitlines():
    event = json.loads(line)
    trace_id = event.get('id')
    if re.fullmatch(r'[0-9a-f]{32}', str(trace_id or '')):
        if trace_id in seen:
            raise SystemExit('episode_candidates=refused: duplicate source trace ID')
        seen.add(trace_id)
    app = event.get('target_app')
    view = event.get('target_view')
    if (not re.fullmatch(r'[0-9a-f]{32}', str(trace_id or '')) or
            app not in apps or
            (view is not None and (app != 'Finder' or view not in views)) or
            event.get('task') != (f'Click {view} in the Finder sidebar' if view else dock_tasks[app])):
        continue
    if event.get('executed') is True:
        outcome = ('verified_success' if event.get('verified') is True else
                   'verified_miss' if event.get('verified') is False else 'unverified')
    elif event.get('action') == 'done' and event.get('verified') is True:
        outcome = 'already_complete'
    elif event.get('action') == 'abstain':
        outcome = 'abstain'
    else:
        outcome = 'unverified'
    review = reviews.get(trace_id, {})
    accepted = (review.get('decision') == 'accept' and outcome == 'verified_success'
                and isinstance(event.get('frame_sha256'), str)
                and isinstance(event.get('after_sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', event['frame_sha256'])
                and re.fullmatch(r'[0-9a-f]{64}', event['after_sha256'])
                and review.get('frame_sha256') == event.get('frame_sha256')
                and review.get('after_sha256') == event.get('after_sha256'))
    before = event.get('frame_sha256')
    after = event.get('after_sha256')
    row = {
        'schema': 'computer-use.episode-candidate/v1', 'episode_id': trace_id,
        'app_id': apps[app], 'app_profile_id': None,
        'surface_id': 'finder.sidebar' if view else 'macos.dock',
        'task_family': 'navigate_location' if view else 'open_app',
        'goal_id': ('finder.' + view.lower()) if view else
                   ('open.' + apps[app].lower().replace('.', '_')),
        'outcome': outcome,
        'verifier': 'guest_ocr' if type(event.get('verified')) is bool else None,
        'review': 'accepted_success' if accepted else
                  'rejected' if review.get('decision') == 'reject' else 'pending',
        'training_rights': 'unknown', 'split': 'unassigned',
        'before_sha256': before if isinstance(before, str) and re.fullmatch(r'[0-9a-f]{64}', before) else None,
        'after_sha256': after if isinstance(after, str) and re.fullmatch(r'[0-9a-f]{64}', after) else None,
    }
    rows.append(row)
    counts[outcome] += 1
if not rows:
    raise SystemExit('episode_candidates=unavailable: no known guest tasks')
root.mkdir(mode=0o700, parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=root,
                                 prefix='.episode-candidates-', suffix='.jsonl', delete=False) as stream:
    os.chmod(stream.name, 0o600)
    for row in rows:
        stream.write(json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n')
    temporary = stream.name
os.replace(temporary, root / 'episode-candidates.jsonl')
print(json.dumps({'episode_candidates': len(rows), 'by_outcome': dict(sorted(counts.items())),
                  'metadata_only': True, 'style_included': False,
                  'ready_for_training': False, 'uploaded': False}, sort_keys=True))
PY
  exit 0
fi
python3 - "$root" "${1:-}" "${2:-}" <<'PY'
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(sys.argv[1]).resolve()
readiness = sys.argv[2] == '--readiness'
if sys.argv[2] == '--record-style':
    allowed = {
        'intent_first', 'organized_work', 'outcome_evidence',
        'adaptive_workflow', 'avoid_repetitive_tasks',
        'private_minimal_capture', 'existing_shell_entrypoints',
        'autonomous_follow_through',
    }
    signals = sys.argv[3].split(',')
    if not signals or len(signals) != len(set(signals)) or not set(signals) <= allowed:
        raise SystemExit('Style signals must be unique allowed pattern IDs')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    profile = {
        'format': 'virtualos-intent-style-v1',
        'scope': 'computer_use_task_policy',
        'source': 'summarized_user_instructions',
        'signals': sorted(signals),
        'capture_exclusions': ['raw_conversation', 'secrets', 'unreviewed_screenshots'],
        'cloud_export': False,
        'review_required_before_training': True,
    }
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=root,
                                     prefix='.style-', suffix='.json', delete=False) as stream:
        os.chmod(stream.name, 0o600)
        json.dump(profile, stream, sort_keys=True, separators=(',', ':'))
        stream.write('\n')
        temporary = stream.name
    os.replace(temporary, root / 'style-profile.json')
    print(json.dumps({'recorded_style_patterns': len(signals), 'cloud_export': False,
                      'raw_conversation_saved': False}))
    raise SystemExit(0)
journal = root / 'steps.jsonl'
review_file = root / 'reviews.jsonl'
if not journal.is_file() or not review_file.is_file():
    raise SystemExit('Accepted trace journal and reviews are required')

reviews = {}
for line in review_file.read_text(encoding='utf-8').splitlines():
    item = json.loads(line)
    reviews[item['id']] = item

def checked_frame(relative, expected_hash):
    if not isinstance(relative, str) or not re.fullmatch(r'frames/[0-9a-f]{32}(?:-after)?\.jpg', relative):
        raise ValueError('Invalid trace image path')
    path = root / relative
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise ValueError('Trace image missing or hash mismatch')
    return path

def dimensions(path):
    result = subprocess.run(['sips', '-g', 'pixelWidth', '-g', 'pixelHeight', str(path)],
                            capture_output=True, text=True, check=True)
    width = re.search(r'pixelWidth: (\d+)', result.stdout)
    height = re.search(r'pixelHeight: (\d+)', result.stdout)
    if not width or not height:
        raise ValueError('Could not read trace image dimensions')
    return int(width.group(1)), int(height.group(1))

examples = []
for line in journal.read_text(encoding='utf-8').splitlines():
    event = json.loads(line)
    review = reviews.get(event.get('id'))
    if not review or review.get('decision') != 'accept':
        continue
    if not (event.get('executed') is True and event.get('verified') is True
            and event.get('action') == 'click' and event.get('target_app')
            and event.get('active_app_after') == event.get('target_app')
            and review.get('frame_sha256') == event.get('frame_sha256')
            and review.get('after_sha256') == event.get('after_sha256')):
        raise ValueError(f'Accepted trace is not a verified click: {event.get("id")}')
    before = checked_frame(event.get('frame'), event.get('frame_sha256'))
    checked_frame(event.get('after_frame'), event.get('after_sha256'))
    width, height = dimensions(before)
    point = event.get('coordinates') or {}
    x, y = point.get('x'), point.get('y')
    if not (isinstance(x, (int, float)) and isinstance(y, (int, float))
            and 0 <= x < width and 0 <= y < height):
        raise ValueError(f'Invalid accepted click: {event.get("id")}')
    proposed = event.get('proposed_coordinates') or {}
    px, py = proposed.get('x'), proposed.get('y')
    valid_proposal = (type(px) in (int, float) and type(py) in (int, float)
                      and 0 <= px < width and 0 <= py < height)
    proposal_relation = ('no_valid_model_point' if not valid_proposal else
                         'model_point_executed' if px == x and py == y else
                         'guard_corrected')
    examples.append({
        'id': event['id'], 'task': event['task'], 'target_app': event['target_app'],
        'target_view': event.get('target_view'),
        'action_source': event.get('action_source') or
                         ('ocr' if event.get('target_view') else 'model_tooltip_guarded'),
        'proposal_relation': proposal_relation,
        'image': event['frame'], 'image_sha256': event['frame_sha256'],
        'image_width': width, 'image_height': height,
        'action': {'type': 'click', 'point_2d': [round(x * 1000 / (width - 1)),
                                                   round(y * 1000 / (height - 1))]},
        'outcome': 'visually_accepted_verified_click',
    })

if not examples:
    raise SystemExit('No manually accepted examples; dataset was not changed')

if readiness:
    by_task = Counter(item['task'] for item in examples)
    by_source = Counter(item['action_source'] for item in examples)
    by_proposal_relation = Counter(item['proposal_relation'] for item in examples)
    image_hashes = {item['image_sha256'] for item in examples}
    print(json.dumps({
        'reviewed_examples': len(examples),
        'unique_before_images': len(image_hashes),
        'duplicate_before_images': len(examples) - len(image_hashes),
        'by_task': dict(sorted(by_task.items())),
        'by_action_source': dict(sorted(by_source.items())),
        'by_proposal_relation': dict(sorted(by_proposal_relation.items())),
        'has_negative_or_abstention_examples': False,
        'has_held_out_evaluation_set': False,
        'ready_for_model_training': False,
        'has_private_style_profile': (root / 'style-profile.json').is_file(),
        'reason': 'Only accepted click examples are curated; add negative examples and a held-out evaluation set before a training job.',
        'dataset_changed': False,
        'uploaded': False,
    }, sort_keys=True))
    raise SystemExit(0)

with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=root, prefix='.dataset-',
                                 suffix='.jsonl', delete=False) as stream:
    os.chmod(stream.name, 0o600)
    for item in examples:
        stream.write(json.dumps(item, separators=(',', ':')) + '\n')
    temp_path = Path(stream.name)
output = root / 'dataset.jsonl'
os.replace(temp_path, output)
print(json.dumps({'examples': len(examples), 'dataset': str(output), 'uploaded': False}))
PY
