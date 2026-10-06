#!/usr/bin/env bash
# Bounded Finder sidebar journey using guest pixels, model proposals, and OCR checks.
set -euo pipefail

target=""
offline_only=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --one-shot) export VOS_CUA_ONE_SHOT=1; shift ;;
    --offline-only) offline_only=1; shift ;;
    --target)
      [[ $# -ge 2 && "$2" =~ ^(Applications|Downloads|Documents|Desktop|Recents)$ ]] || {
        echo 'Usage: ./tools/computer-journey.sh [--one-shot] [--offline-only --target Applications|Downloads|Documents|Desktop|Recents]' >&2
        exit 2
      }
      target="$2"
      shift 2
      ;;
    *)
      echo 'Usage: ./tools/computer-journey.sh [--one-shot] [--offline-only --target Applications|Downloads|Documents|Desktop|Recents]' >&2
      exit 2
      ;;
  esac
done
if (( offline_only )) && [[ -z "$target" ]]; then
  echo 'journey=refused: offline-only requires one target' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if pgrep -fl '[v]osctl computer-night' >/dev/null 2>&1; then
  echo 'journey=refused: stop the overnight runner first' >&2
  exit 1
fi
guard=--guard
(( offline_only )) && guard=--guard-offline
if ! "$repo_dir/tools/computer-resources.sh" "$guard" >/dev/null; then
  echo 'journey=paused: Mini resource guard' >&2
  exit 1
fi
if (( offline_only )); then
  python3 -B - "$repo_dir/tools" "$target" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from computer_use import _offline_sidebar_suggestion
if _offline_sidebar_suggestion(sys.argv[2])['choice'] != 'finder_sidebar_click':
    raise SystemExit('journey=paused: no learned Finder action for this target')
PY
fi
"$repo_dir/tools/computer-wake.sh"
"$repo_dir/tools/computer-unlock.sh"
if (( offline_only )); then
  "$repo_dir/tools/computer-check.sh" --no-model
else
  "$repo_dir/tools/computer-check.sh"
fi
if ! "$repo_dir/tools/vosctl" computer doctor \
  | python3 -c 'import json,sys; s=json.load(sys.stdin); sys.exit(0 if s.get("active_app")=="Finder" else 1)'; then
  if (( offline_only )); then
    python3 -B - "$repo_dir/tools" <<'PY'
import hashlib, sys, time, uuid
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from computer_use import (GuestVM, _active_guest_app, _confirmed_dock_click,
                          _offline_dock_feedback, _record_local_step)
vm = GuestVM()
frame = vm.frame()
if _active_guest_app(frame) != 'Terminal':
    raise SystemExit('journey=paused: expected Terminal before guarded Finder activation')
point = _confirmed_dock_click(vm, frame, {'x': round(frame.width * 0.333),
                                           'y': round(frame.height * 0.94)}, 'Finder')
if point is None:
    raise SystemExit('journey=paused: Finder Dock tooltip was not confirmed')
vm.click(point['x'], point['y'], frame=frame)
time.sleep(1)
after = vm.frame()
verified = _active_guest_app(after) == 'Finder'
event = {'id': uuid.uuid4().hex, 'ts': time.time(), 'task': 'finder_activate',
         'action': 'click', 'action_source': 'offline_locator_tooltip_guarded',
         'target_app': 'Finder', 'active_app_before': 'Terminal',
         'active_app_after': _active_guest_app(after), 'executed': True,
         'verified': verified, 'coordinates': {'x': point['x'], 'y': point['y']},
         'frame_sha256': hashlib.sha256(frame.jpeg).hexdigest(), 'mode': 'act'}
root = Path.home() / 'Library/Application Support/virtualOS/computer-use'
_record_local_step(root, frame, event, after)
_offline_dock_feedback('Finder', event, frame)
if not verified:
    raise SystemExit('journey=paused: Finder did not become active')
print('journey=finder_activated tooltip_guarded=true model_calls=0')
PY
  else
    "$repo_dir/tools/computer-verify.sh" --act 'Click the blue smiling Finder icon in the guest Dock'
  fi
fi
"$repo_dir/tools/vosctl" shortcut n command >/dev/null
sleep 1

python3 - "$repo_dir/tools" "$target" "$offline_only" <<'PY'
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, sys.argv[1])
from computer_use import (GuestVM, _active_guest_app, _finder_label_center,
                          _finder_model_action,
                          _finder_point_in_label, _finder_sidebar_label,
                          _finder_view_visible, _offline_sidebar_feedback,
                          _offline_sidebar_suggestion, _record_local_step)

vm = GuestVM()
model = os.getenv('VOS_OLLAMA_MODEL', 'qwen3.5:0.8b')
root = Path.home() / 'Library/Application Support/virtualOS/computer-use/journeys'
target_only = sys.argv[2]
offline_only = sys.argv[3] == '1'

if offline_only:
    advice = _offline_sidebar_suggestion(target_only)
    if advice['choice'] != 'finder_sidebar_click':
        raise SystemExit('journey=paused: local policy has no supported action')

if target_only:
    # Start from a different verified view so a model "done" is not a trivial
    # answer and the trace tests an actual transition.
    start = 'Applications' if target_only != 'Applications' else 'Documents'
    frame = vm.frame()
    if offline_only and _active_guest_app(frame) != 'Finder':
        raise SystemExit('journey=paused: Finder is not active')
    label = _finder_sidebar_label(frame, start)
    if label is None:
        raise SystemExit(f'journey=stopped: {start} sidebar label is not uniquely visible')
    sx, sy = _finder_label_center(frame, label)
    if shutil.disk_usage(root.parent).free < 3 * 1024 ** 3:
        raise SystemExit('journey=stopped: disk fell below 3 GiB before start click')
    vm.click(sx, sy, frame=frame)
    for delay in (0.5, 1, 2, 3):
        time.sleep(delay)
        if _finder_view_visible(vm.frame(), start):
            break
    else:
        raise SystemExit(f'journey=stopped: could not verify starting view {start}')

for target in ((target_only,) if target_only else ('Applications', 'Downloads', 'Documents')):
    if shutil.disk_usage(root.parent).free < 3 * 1024 ** 3:
        raise SystemExit('journey=stopped: disk fell below 3 GiB')
    task = f'Click {target} in the Finder sidebar'
    frame = vm.frame()
    if _active_guest_app(frame) != 'Finder':
        raise SystemExit('journey=stopped: Finder is not active')
    label = _finder_sidebar_label(frame, target)
    if label is None:
        raise SystemExit(f'journey=stopped: {target} sidebar label is not uniquely visible')
    try:
        advice = _offline_sidebar_suggestion(target)
    except (OSError, ValueError, sqlite3.Error):
        advice = None
    if advice:
        counts = advice['counts'].get('finder_sidebar_click', {})
        failures = counts.get('failure', 0)
        successes = counts.get('success', 0)
        if failures >= 3 and failures * 5 >= 4 * (failures + successes):
            raise SystemExit(f'journey=stopped: {target} local outcomes failed repeatedly')
    learned = bool(advice and advice['choice'] == 'finder_sidebar_click')
    if offline_only and not learned:
        raise SystemExit('journey=paused: local policy changed before action')
    if learned:
        # The OCR row is still located in the current frame; history only
        # selects the reviewed action and avoids another model request.
        action, first_model_action, retry_reason = ({'type': 'click'}, 'offline_policy', None)
    else:
        action, first_model_action, retry_reason = _finder_model_action(frame, target, model, label)
    model_false_done = retry_reason == 'false_done'
    x, y = _finder_label_center(frame, label)
    proposed = ({'x': action['x'], 'y': action['y']}
                if action.get('type') == 'click'
                and type(action.get('x')) in (int, float)
                and type(action.get('y')) in (int, float)
                and math.isfinite(action['x']) and math.isfinite(action['y'])
                else None)
    proposal_near = (None if learned else proposed is not None
                     and math.hypot(proposed['x'] - x, proposed['y'] - y) <= 120)
    model_hit = bool(proposed and _finder_point_in_label(frame, label,
                                                         proposed['x'], proposed['y']))
    click_x, click_y = ((proposed['x'], proposed['y']) if model_hit else (x, y))
    # The model controls the click only inside the visible target's hit region.
    # Otherwise the OCR center supplies the safe click and the trace records it.
    if shutil.disk_usage(root.parent).free < 3 * 1024 ** 3:
        raise SystemExit('journey=stopped: disk fell below 3 GiB before model click')
    vm.click(click_x, click_y, frame=frame)
    after = None
    verified = False
    for delay in (0.5, 1, 2, 3):
        time.sleep(delay)
        after = vm.frame()
        if _finder_view_visible(after, target):
            verified = True
            break
    event = {
        'id': uuid.uuid4().hex, 'ts': time.time(), 'task': task,
        'model': None if learned else model, 'action': 'click',
        'model_action': None if learned else action.get('type'),
        'model_first_action': None if learned else first_model_action,
        'model_false_done': model_false_done,
        'model_retry_reason': retry_reason,
        'model_calls': 0 if learned else (2 if retry_reason else 1),
        'frame_sha256': hashlib.sha256(frame.jpeg).hexdigest(),
        'proposed_coordinates': proposed, 'coordinates': {'x': click_x, 'y': click_y},
        'proposal_near_target': proposal_near,
        'proposal_in_hit_region': None if learned else model_hit,
        'corrected_by_ocr': not learned and not model_hit,
        'action_source': 'offline_policy_label_guarded' if learned else ('model' if model_hit else 'ocr'),
        'target_app': 'Finder', 'target_view': target, 'executed': True,
        'changed': after is not None and after.jpeg != frame.jpeg,
        'active_app_before': 'Finder',
        'active_app_after': _active_guest_app(after) if after else None,
        'verified': verified, 'mode': 'act',
    }
    _record_local_step(root, frame, event, after)
    try:
        _offline_sidebar_feedback(target, event)
    except (OSError, ValueError, sqlite3.Error):
        pass
    print(json.dumps({'target_view': target, 'verified': verified,
                      'model_action': None if learned else action.get('type'),
                      'model_calls': 0 if learned else (2 if retry_reason else 1),
                      'model_false_done': model_false_done,
                      'model_retry_reason': retry_reason,
                      'proposal_near_target': proposal_near,
                      'proposal_in_hit_region': None if learned else model_hit,
                      'action_source': event['action_source'],
                      'corrected_by_ocr': not learned and not model_hit}), flush=True)
    if not verified:
        raise SystemExit('journey=stopped: Finder view was not verified')
print('journey=complete')
PY
