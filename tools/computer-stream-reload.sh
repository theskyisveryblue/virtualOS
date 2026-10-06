#!/usr/bin/env bash
# Guarded reload of the independent ai-share screenshot viewer on the Mini.
set -euo pipefail
[[ $# -eq 1 && ( "$1" == --status || "$1" == --canary || "$1" == --reload ) ]] || {
  echo 'Usage: ./tools/computer-stream-reload.sh --status|--canary|--reload' >&2; exit 2;
}
python3 - "$1" <<'PY'
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

mode = sys.argv[1]
if os.geteuid() != 501 or os.getenv('USER') != 'ai':
    raise SystemExit('stream_reload=refused: Mini ai account required')
home = Path('/Users/ai')
repo = home / 'Documents/GitHub/ai-share/virtualos_remote/server.py'
runtime = home / 'Library/Application Support/ai-share-local-ai-node/runtime/ai-share/virtualos_remote/server.py'
candidate = Path('/private/tmp/ai-share-server-optimized.py')
server_python = Path('/opt/homebrew/bin/python3')
baseline_hash = '1efefc0b04584220ecd36a4a45eb8da13158b715f4b042af68ba56c3be307fca'
candidate_hash = 'd31da7f26560422ca494e24d6b6fc84ac240458d0e045a47e8fdc1bb13945430'
canary_port = 18998

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

if any(not p.is_file() or p.is_symlink() for p in (repo, runtime)):
    raise SystemExit('stream_reload=refused: source path changed')
source_hashes = [digest(p) for p in (repo, runtime)]
if source_hashes not in ([baseline_hash, baseline_hash],
                         [candidate_hash, candidate_hash]):
    raise SystemExit('stream_reload=refused: source hashes differ or changed')

pids = subprocess.check_output(['lsof', '-nP', '-t', '-iTCP:8899', '-sTCP:LISTEN'],
                               text=True, timeout=5).split()
if len(pids) != 1 or not pids[0].isdigit():
    raise SystemExit('stream_reload=refused: expected one live stream process')
old_pid = int(pids[0])
command = subprocess.check_output(['ps', '-p', str(old_pid), '-o', 'command='],
                                  text=True, timeout=5)
if str(runtime) not in command or '--port 8899' not in command:
    raise SystemExit('stream_reload=refused: listener identity mismatch')
process = subprocess.check_output(['ps', 'eww', '-p', str(old_pid), '-o', 'command='],
                                  text=True, timeout=5)
match = re.search(r'(?:^|\s)VOS_AUTH_TOKEN=(\S+)', process)
if not match or len(match.group(1)) < 16:
    raise SystemExit('stream_reload=refused: live auth token unavailable')
environment = os.environ.copy()
environment['VOS_AUTH_TOKEN'] = match.group(1)
for key in ('VOS_WATCH_REPOS', 'VOS_REPO_POLL_INTERVAL'):
    value = re.search(r'(?:^|\s)' + key + r'=(\S+)', process)
    if value:
        environment[key] = value.group(1)

def get(path, port):
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers={
        'Authorization': 'Bearer ' + environment['VOS_AUTH_TOKEN']})
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.read()

def ready(port, viewer=False):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            if b'"ok": true' in get('/health', port):
                page = get('/debug', port)
                if not viewer or (b'let inFlight = false' in page and b'min="500"' in page):
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    return False

if not ready(8899):
    raise SystemExit('stream_reload=refused: current stream health or auth failed')
if source_hashes == [candidate_hash, candidate_hash]:
    if not ready(8899, viewer=True):
        raise SystemExit('stream_reload=refused: optimized source is not live')
    print('stream_status=optimized auth=required viewer=single_request')
    raise SystemExit(0)
if mode == '--status':
    print('stream_status=baseline auth=required viewer=fixed_interval')
    raise SystemExit(0)
if not candidate.is_file() or candidate.is_symlink() or digest(candidate) != candidate_hash:
    raise SystemExit('stream_reload=refused: candidate hash mismatch')
if not server_python.is_file():
    raise SystemExit('stream_reload=refused: server Python unavailable')
with socket.socket() as probe:
    probe.settimeout(1)
    if probe.connect_ex(('127.0.0.1', canary_port)) == 0:
        raise SystemExit('stream_reload=refused: canary port occupied')

def launch(path, port, output):
    return subprocess.Popen([str(server_python), str(path), '--host',
                             '127.0.0.1' if port == canary_port else '0.0.0.0',
                             '--port', str(port), '--no-bonjour'],
                            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                            env=environment, start_new_session=True,
                            cwd=str(path.parent))

log_path = home / 'Library/Logs/virtualos-stream-reload.log'
log_path.parent.mkdir(parents=True, exist_ok=True)
with log_path.open('ab') as log:
    canary = launch(candidate, canary_port, log)
    try:
        if not ready(canary_port, viewer=True):
            raise SystemExit('stream_reload=failed: canary health, auth, or viewer check')
    finally:
        canary.terminate()
        try:
            canary.wait(timeout=5)
        except subprocess.TimeoutExpired:
            canary.kill()
            canary.wait(timeout=5)
    print('stream_canary=passed auth=required viewer=single_request min_interval_ms=500')
    if mode == '--canary':
        raise SystemExit(0)

    backups = {path: path.read_bytes() for path in (repo, runtime)}
    old_stopped = False
    try:
        for path in (repo, runtime):
            temporary = path.with_name(path.name + '.vos-reload-tmp')
            shutil.copy2(candidate, temporary)
            os.replace(temporary, path)
        os.kill(old_pid, signal.SIGTERM)
        for _ in range(40):
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError('old stream process did not exit')
        old_stopped = True
        replacement = launch(runtime, 8899, log)
        if not ready(8899, viewer=True):
            replacement.terminate()
            try:
                replacement.wait(timeout=5)
            except subprocess.TimeoutExpired:
                replacement.kill()
                replacement.wait(timeout=5)
            raise RuntimeError('new stream process did not verify')
        print(f'stream_reload=passed pid={replacement.pid} auth=required viewer=optimized')
    except Exception as error:
        for path, data in backups.items():
            path.write_bytes(data)
        fallback = launch(runtime, 8899, log) if old_stopped else None
        restored = ready(8899)
        print(f'stream_reload=failed rolled_back={str(restored).lower()} fallback_pid={fallback.pid if fallback else old_pid} reason={error}',
              file=sys.stderr)
        raise SystemExit(1)
PY
