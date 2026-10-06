#!/usr/bin/env bash
# Unlock only when guest OCR confirms the central macOS password screen.
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo 'Usage: ./tools/computer-unlock.sh' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
locked=0
for delay in 0 1 2 3; do
  sleep "$delay"
  state="$("$repo_dir/tools/vosctl" computer doctor)"
  if printf '%s' "$state" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("active_app") else 1)'; then
    echo 'guest_desktop=already_unlocked'
    exit 0
  fi
  if printf '%s' "$state" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("guest_locked") is True else 1)'; then
    locked=1
    break
  fi
done
if (( ! locked )); then
  echo 'guest_desktop=unconfirmed; password not sent' >&2
  exit 1
fi
if [[ -z "${VOS_VM_PASSWORD:-}" ]] && ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?VOS_VM_PASSWORD=.+$' "$HOME/.config/virtualos/start.env" 2>/dev/null; then
  echo 'guest_password=unavailable' >&2
  exit 1
fi
echo 'guest_desktop=locked; sending configured guest unlock'
python3 - "$HOME/.config/virtualos/start.env" <<'PY'
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.request import Request, urlopen

password = os.getenv('VOS_VM_PASSWORD', '')
if not password:
    for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
        match = re.match(r'^\s*(?:export\s+)?VOS_VM_PASSWORD=(.*)$', line)
        if match:
            password = match.group(1).strip().strip('"\'')
if not re.fullmatch(r'[a-z0-9]+', password):
    raise SystemExit('Guest password needs a supported key sequence; no input sent')

def post(path, payload):
    request = Request('http://127.0.0.1:8845' + path,
                      data=json.dumps(payload).encode('utf-8'),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=10) as response:
        result = json.load(response)
    if result.get('ok') is not True:
        raise RuntimeError('Guest input request failed')

with urlopen('http://127.0.0.1:8845/vm/screen', timeout=10) as response:
    width = int(response.headers['X-Capture-Width'])
    height = int(response.headers['X-Capture-Height'])
post('/vm/click', {'x': round(width * 0.5), 'y': round(height * 0.1),
                   'button': 'left', 'count': 1})
time.sleep(0.8)
for char in password:
    post('/vm/key', {'key': char})
    time.sleep(0.12)
time.sleep(0.3)
post('/vm/key', {'key': 'return'})
PY
for delay in 3 4 5; do
  sleep "$delay"
  state="$("$repo_dir/tools/vosctl" computer doctor)"
  if printf '%s' "$state" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("active_app") else 1)'; then
    echo 'guest_desktop=unlocked'
    exit 0
  fi
done
echo 'guest_desktop=still_locked' >&2
exit 1
