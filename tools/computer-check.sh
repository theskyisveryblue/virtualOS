#!/usr/bin/env bash
# Read-only preflight. No guest input and no model request.
set -euo pipefail

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --benchmark && "$1" != --build-ocr && "$1" != --retention-self-test && "$1" != --no-model ) ]]; then
  echo "Usage: ./tools/computer-check.sh [--build-ocr|--benchmark|--retention-self-test|--no-model]" >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${1:-}" == --retention-self-test ]]; then
  python3 -B - "$repo_dir/tools" <<'PY'
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from computer_use import Frame, _record_local_step

before = Frame(b'before', 8, 8, 8, 8)
after = Frame(b'after', 8, 8, 8, 8)
with tempfile.TemporaryDirectory(prefix='vos-retention-', dir='/private/tmp') as directory:
    root = Path(directory)
    _record_local_step(root, before, {'id': 'a' * 32, 'executed': False, 'action': 'abstain'})
    _record_local_step(root, before, {'id': 'b' * 32, 'executed': True, 'action': 'click'}, after)
    rows = [json.loads(line) for line in (root / 'steps.jsonl').read_text().splitlines()]
    assert 'frame' not in rows[0] and 'after_frame' not in rows[0]
    assert sorted(path.name for path in (root / 'frames').iterdir()) == [
        'b' * 32 + '-after.jpg', 'b' * 32 + '.jpg']
    assert rows[1]['frame'] and rows[1]['after_frame']
print('retention_self_test=passed nonexecuted_frames=0 executed_frames=2')
PY
  exit 0
fi
if [[ "${1:-}" == --build-ocr ]]; then
  helper="$repo_dir/tools/virtualos_stream_server/inputctl"
  source="$repo_dir/tools/virtualos_stream_server/inputctl.swift"
  candidate="${helper}.next.$$"
  trap 'rm -f "$candidate"' EXIT
  swiftc -O "$source" -o "$candidate"
  chmod 755 "$candidate"
  mv -f "$candidate" "$helper"
  echo "ocr_binary=built architecture=$(uname -m)"
  exit 0
fi
if [[ "${1:-}" == --benchmark ]]; then
  python3 - "$repo_dir/tools/virtualos_stream_server/inputctl" <<'PY'
import json, pathlib, statistics, subprocess, sys, tempfile, time, urllib.request

helper = sys.argv[1]
url = 'http://127.0.0.1:8845/vm/screen'
screens = []
with tempfile.TemporaryDirectory(prefix='vos-bench-', dir='/private/tmp') as temp:
    frame = pathlib.Path(temp) / 'guest.jpg'
    for _ in range(5):
        started = time.monotonic()
        with urllib.request.urlopen(url, timeout=10) as response:
            data = response.read()
        screens.append(round(time.monotonic() - started, 3))
        frame.write_bytes(data)
    results = {}
    for mode in ('ocr-fast', 'ocr'):
        runs = []
        matches = set()
        for _ in range(3):
            started = time.monotonic()
            completed = subprocess.run([helper, mode, str(frame)],
                                       check=True, capture_output=True, text=True, timeout=30)
            runs.append(round(time.monotonic() - started, 3))
            lines = [item.get('text', '').lower() for item in json.loads(completed.stdout)]
            for name in ('finder', 'terminal', 'safari', 'chrome'):
                if any(name in line for line in lines):
                    matches.add(name)
        results[mode] = {'seconds': runs,
                         'median_seconds': round(statistics.median(runs), 3),
                         'app_names_seen': sorted(matches)}
print(json.dumps({'benchmark': 'read_only_guest_capture',
                  'screenshot_seconds': screens,
                  'screenshot_median_seconds': round(statistics.median(screens), 3),
                  'ocr': results, 'image_or_text_saved': False}, sort_keys=True))
PY
  exit 0
fi

floor_kb="${VOS_CUA_MIN_FREE_KB:-3145728}"
[[ "$floor_kb" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid VOS_CUA_MIN_FREE_KB' >&2; exit 2; }
"$repo_dir/tools/computer-status.sh"
free_kb="$(df -Pk /System/Volumes/Data | awk 'NR == 2 {print $4}')"
if [[ ! "$free_kb" =~ ^[0-9]+$ ]] || (( free_kb < floor_kb )); then
  echo "preflight=failed: disk below ${floor_kb} KiB" >&2
  exit 1
fi
if [[ -e "$HOME/.config/virtualos/computer-stop" && "${VOS_CUA_ONE_SHOT:-0}" != 1 ]]; then
  echo 'preflight=failed: computer-stop file exists' >&2
  exit 1
fi
if [[ ! -x "$repo_dir/tools/virtualos_stream_server/inputctl" ]]; then
  echo 'preflight=failed: guest OCR helper missing' >&2
  exit 1
fi
if [[ "$repo_dir/tools/virtualos_stream_server/inputctl.swift" -nt "$repo_dir/tools/virtualos_stream_server/inputctl" ]]; then
  echo 'preflight=failed: guest OCR helper is older than its source; run ./tools/computer-check.sh --build-ocr' >&2
  exit 1
fi
if ! health_json="$(curl -fsS --max-time 3 http://127.0.0.1:8845/vm/health 2>/dev/null)" \
    || ! printf '%s' "$health_json" | python3 -c 'import json,sys
try: h=json.load(sys.stdin)
except (ValueError, TypeError): h={}
sys.exit(0 if isinstance(h,dict) and h.get("ready") and h.get("state")=="running" else 1)'; then
  echo 'preflight=failed: guest unavailable' >&2
  exit 1
fi
if [[ "${1:-}" != --no-model ]]; then
  if ! curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null; then
    echo 'preflight=failed: Ollama unavailable' >&2
    exit 1
  fi
  model="${VOS_OLLAMA_MODEL:-qwen3.5:0.8b}"
  if ! tags_json="$(curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags 2>/dev/null)" \
    || ! printf '%s' "$tags_json" | python3 -c 'import json,sys
try: tags=json.load(sys.stdin)
except (ValueError, TypeError): tags={}
models=tags.get("models",[]) if isinstance(tags,dict) else []
sys.exit(0 if any(isinstance(x,dict) and x.get("name")==sys.argv[1] for x in models) else 1)' "$model"; then
    echo "preflight=failed: model $model unavailable" >&2
    exit 1
  fi
fi
python3 - "$repo_dir/tools/computer_use.py" <<'PY'
from pathlib import Path
import sys
compile(Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec')
PY
if ! "$repo_dir/tools/vosctl" computer doctor \
    | python3 -c 'import json,sys; s=json.load(sys.stdin); sys.exit(0 if s.get("ready") and s.get("screenshot_usable") and s.get("active_app") else 1)'; then
  echo 'preflight=failed: guest desktop unconfirmed' >&2
  exit 1
fi
echo 'preflight=passed'
