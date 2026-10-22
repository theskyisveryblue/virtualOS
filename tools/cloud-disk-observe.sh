#!/usr/bin/env bash
# Read-only Mac mini and archive conversion status. Run with no arguments.
set -euo pipefail
umask 077
[[ $# -eq 0 ]] || { echo 'Usage: ./tools/cloud-disk-observe.sh' >&2; exit 2; }
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${VOS_ARCHIVE_OBSERVER_RUNNER:-}" != 1 ]]; then
  exec python3 - "$repo_dir" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

root = Path(sys.argv[1])
directory = Path('/private/tmp/virtualos-archive-conversion')
if directory.is_symlink():
    raise SystemExit('observer refused: linked state directory')
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
info = directory.stat()
if info.st_uid != os.getuid() or info.st_mode & 0o077:
    raise SystemExit('observer refused: state directory must be private and owned by this user')
for name in ('observer-launch.lock', 'observer.pid', 'observer.log',
             'observer-run.lock', 'observer-latest.txt', 'observer-next.txt'):
    if (directory / name).is_symlink():
        raise SystemExit('observer refused: linked state file')
    if (directory / name).exists():
        info = (directory / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise SystemExit('observer refused: state file must be regular and owned by this user')
        (directory / name).chmod(0o600)
with (directory / 'observer-launch.lock').open('w') as lock, \
     (directory / 'observer-run.lock').open('a') as runner_lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    pid_file = directory / 'observer.pid'
    try:
        fcntl.flock(runner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        alive = False
    except BlockingIOError:
        alive = True
    pid = None
    if alive and pid_file.exists():
        try:
            pid = int(pid_file.read_text())
        except (OSError, ValueError):
            pass
    if not alive:
        environment = {key: value for key, value in os.environ.items()
                       if not (key.startswith(('AWS_', 'CLOUDFLARE_', 'CF_API_')) or
                               any(word in key.upper() for word in ('SECRET', 'TOKEN', 'PASSWORD', 'API_KEY')))}
        environment['VOS_ARCHIVE_OBSERVER_RUNNER'] = '1'
        with (directory / 'observer.log').open('a') as log:
            process = subprocess.Popen(['bash', str(root / 'tools/cloud-disk-observe.sh')],
                env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, pass_fds=(runner_lock.fileno(),))
        pid = process.pid
        pid_file.write_text(str(pid) + '\n')
    snapshot = directory / 'observer-latest.txt'
    log = directory / 'observer.log'
    age = max(0, time.time() - snapshot.stat().st_mtime) if snapshot.exists() else None
    print(json.dumps({'observer': 'running', 'pid': pid,
                      'log': str(log), 'snapshot_age_seconds': age}), flush=True)
    if snapshot.exists():
        print(snapshot.read_text().strip())
    elif log.exists():
        with log.open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 4000))
            recent = stream.read().decode(errors='replace')
            if 'observed_at=' in recent:
                recent = 'observed_at=' + recent.rsplit('observed_at=', 1)[1]
            print(recent.strip())
PY
fi
snapshot_dir=/private/tmp/virtualos-archive-conversion
while true; do
python3 - "$repo_dir" > "$snapshot_dir/observer-next.txt" 2>&1 <<'PY' || true
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'tools'))
from cloud_disk_connections import control_options, relay_status
print('observed_at=' + datetime.datetime.now(datetime.timezone.utc).isoformat(), flush=True)
try:
    from cloud_disk_work_loop import control_observation
    print('agent_control=' + json.dumps(control_observation(
        Path('/private/tmp/virtualos-archive-conversion')), sort_keys=True), flush=True)
except (OSError, ValueError) as failure:
    print('agent_control=' + type(failure).__name__, flush=True)
try:
    workspace = os.statvfs(root)
    temporary = os.statvfs('/private/tmp')
    workspace_free = workspace.f_bavail * workspace.f_frsize
    temporary_free = temporary.f_bavail * temporary.f_frsize
    print('operator_storage_snapshot=' + json.dumps({
        'workspace_available_bytes': workspace_free,
        'temporary_available_bytes': temporary_free,
        'minimum_inference_free_bytes': 8 * 1024**3,
        'minimum_inference_free_met': min(workspace_free, temporary_free) >= 8 * 1024**3,
        'runtime_staging_post_stage_reserve_bytes': 10 * 1024**3,
        'full_inference_admission_checked': False,
        'runtime_staging_admission_checked': False}, sort_keys=True), flush=True)
except OSError as failure:
    print('operator_storage_snapshot=' + type(failure).__name__, flush=True)
try:
    sys.path.insert(0, str(root / 'tools/fleet'))
    from ocr_budget import resources
    operator = resources()
    print('operator_resource_snapshot=' + json.dumps({
        **{key: operator[key] for key in ('available_bytes', 'swap_used_bytes',
                                         'swapouts', 'cpus', 'load')},
        'minimum_available_bytes': 6 * 1024**3,
        'maximum_swap_used_bytes': 8 * 1024**3,
        'minimum_available_met': operator['available_bytes'] >= 6 * 1024**3,
        'swap_requirement_met': operator['swap_used_bytes'] < 8 * 1024**3,
        'paired_swapout_check_performed': False,
        'full_inference_admission_checked': False}, sort_keys=True), flush=True)
except (OSError, ValueError, subprocess.SubprocessError) as failure:
    print('operator_resource_snapshot=' + type(failure).__name__, flush=True)
try:
    rows = subprocess.run(['ps', '-axo', 'pid=,etime=,command='], capture_output=True,
                          text=True, timeout=5, check=True)
    active = []
    for line in rows.stdout.splitlines():
        if any(name in line for name in ('cloud_disk_archive', 'cloud-disk-archive.sh', 'cloud_disk_credentials.py', 'linux_lab_work.py', 'linux_maintained_work.py', 'cloud_disk_work_loop.py')) and 'ps -axo' not in line:
            parts = line.strip().split(None, 2)
            if len(parts) == 3:
                active.append({'pid': parts[0], 'elapsed': parts[1]})
    print('archive_local_processes=' + json.dumps(active), flush=True)
except (OSError, subprocess.SubprocessError) as failure:
    print('archive_local_processes=' + json.dumps({
        'available': False, 'error': type(failure).__name__}), flush=True)
state_dir = Path('/private/tmp/virtualos-archive-conversion')
for filename in ('status.json', 'result.json', 'recovery-watch.json', 'recovery.json', 'recovery-result.json', 'linux-work.json', 'linux-maintained.json', 'work-loop.json'):
    path = state_dir / filename
    if path.is_file():
        try:
            row = json.loads(path.read_text())
            safe = {key: row[key] for key in ('state', 'pid', 'signed_requests', 'requested_upload_bytes',
                    'started_at', 'updated_at', 'archive_bytes', 'logical_bytes', 'chunk_count',
                    'uploaded_bytes', 'reused_bytes', 'readback_verified', 'manifest_sha256',
                    'execution_host', 'upload_workers',
                    'returncode', 'deadline', 'archive_action', 'archive_state', 'linux_state', 'recovery_state',
                    'retryable_connection_failure', 'error', 'remote_error_type', 'remote_error_category',
                    'guest_boots_verified', 'disk_service_restart_verified',
                    'guest_boot_verified', 'full_r2_readback_verified', 'running_macos_guests') if key in row}
            if isinstance(row.get('phase'), dict):
                safe['phase'] = {key: row['phase'][key] for key in ('state', 'verified_bytes',
                    'logical_bytes', 'boot', 'full_r2_readback_verified', 'archive_bytes_read',
                    'uploaded_bytes', 'reused_bytes', 'chunks_verified', 'upload_workers',
                    'attempt', 'retry_budget', 'wait_seconds') if key in row['phase']}
            print(filename + '=' + json.dumps(safe, sort_keys=True), flush=True)
        except (OSError, ValueError):
            print(filename + '=unreadable', flush=True)
# Include prepared evidence in the same no-argument observer, with its own
# timestamp. These receipts describe measured checks, not fresh host settings.
from linux_guest_selection import read_json
for label, filename, fields in (
    ('ocr_prepared_evidence', 'ocr-runner-readiness-20261019.json',
     ('status', 'source_verified_at', 'live_ocr_inference_verified', 'guest_task_verified')),
    ('host_security_last_review', 'mini-host-security-review-20261019.json',
     ('observed_at', 'host_settings_changed', 'firewall_activation_verified'))):
    try:
        receipt = read_json(root/'tools/experiments'/filename, private=False)
        summary = {key:receipt[key] for key in fields if key in receipt}
        if label == 'host_security_last_review':
            summary['observations'] = {key:receipt.get('observations',{}).get(key) for key in
                ('filevault_enabled', 'application_firewall_enabled', 'sip_enabled', 'gatekeeper_assessments_enabled')}
            summary['current_settings_rechecked'] = False
        print(label + '=' + json.dumps(summary,sort_keys=True),flush=True)
    except (OSError,ValueError):
        print(label + '=unavailable',flush=True)
config = Path.home() / 'Documents/GitHub/realestate-machine/.local/mini-remote-20260915/ssh_config'
try:
    print('relay_direct=' + json.dumps(relay_status(), sort_keys=True), flush=True)
except Exception as error:
    from urllib.error import HTTPError
    detail = ' http_status=' + str(error.code) if isinstance(error, HTTPError) else ''
    print('relay_direct=' + type(error).__name__ + detail, flush=True)
ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2']
try:
    ssh += control_options(state_dir)
except (OSError, ValueError) as error:
    print('mini_access=' + type(error).__name__, flush=True)
    sys.exit(1)
if config.is_file():
    ssh += ['-F', str(config), 'mac-mini-cloudflare']
else:
    ssh += ['mac-mini-ai']
remote = '''hostname
df -h /System/Volumes/Data
sysctl vm.swapusage
memory_pressure -Q
sysctl hw.ncpu
uptime
ps -axo pid,etime,rss,comm | awk 'tolower($0) ~ /virtualos|fleet/ {print}'
python3 - <<'HOST_STATUS'
import json, os, pathlib, re, shlex, stat, subprocess, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
def get(url, token=None):
    headers = {'User-Agent': 'virtualos-observer/1.0'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with opener.open(urllib.request.Request(url, headers=headers), timeout=5) as response:
        data = response.read(262145)
    if len(data) > 262144:
        raise ValueError('status response exceeds bound')
    return json.loads(data)
try:
    memory = subprocess.check_output(['vm_stat'], text=True, timeout=5)
    page = re.search(r'page size of ([0-9]+) bytes', memory)
    if page is None or int(page[1]) not in (4096, 16384):
        raise ValueError('memory page size unavailable')
    values = {}
    for key in ('Pages free', 'Pages inactive', 'Pages speculative', 'Swapouts'):
        match = re.search(r'^' + key + r':\\s*([0-9]+)\\.', memory, re.M)
        if match is None:
            raise ValueError('memory counters unavailable')
        values[key] = int(match[1])
    available = sum(values[key] for key in ('Pages free', 'Pages inactive', 'Pages speculative')) * int(page[1])
    print('ocr_memory_snapshot=' + json.dumps({
        'available_bytes': available, 'minimum_available_bytes': 6 * 1024**3,
        'minimum_available_met': available >= 6 * 1024**3,
        'swapouts': values['Swapouts'], 'full_inference_admission_checked': False}, sort_keys=True))
except Exception as error:
    print('ocr_memory_snapshot=' + type(error).__name__)
try:
    loaded = get('http://127.0.0.1:11434/api/ps').get('models')
    if not isinstance(loaded,list) or any(not isinstance(row,dict) for row in loaded):
        raise ValueError('global model status unavailable')
    expirations = [row['expires_at'] for row in loaded if isinstance(row.get('expires_at'),str)
                   and len(row['expires_at']) <= 64]
    print('global_model_residency=' + json.dumps({
        'loaded_model_count':len(loaded), 'idle_requirement_met':not loaded,
        'earliest_reported_expiration':min(expirations) if expirations else None,
        'full_inference_admission_checked':False},sort_keys=True))
except Exception as error:
    print('global_model_residency=' + type(error).__name__)
try:
    fleet = get('http://127.0.0.1:9225/fleet/status')
    print('fleet_status=' + json.dumps({'instances': [
        {key: vm[key] for key in ('name', 'state', 'ready') if key in vm}
        for vm in fleet.get('instances', [])]}, sort_keys=True))
except Exception as error:
    print('fleet_status=' + type(error).__name__)
# storage-snapshot:start
try:
    import sqlite3, time
    source = pathlib.Path.home() / 'vos-fleet/.virtualos-nbd/vm-a/overlay.sqlite'
    info = source.lstat()
    if (source.is_symlink() or any(parent.is_symlink() for parent in source.parents)
            or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_size > 4 * 1024**3):
        raise ValueError('unsafe overlay observation path')
    started = time.monotonic()
    db = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=0.25)
    try:
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(time.monotonic() - started > 0.5), 1000)
        encoded = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='encoded_blocks'").fetchone()
        table = 'encoded_blocks' if encoded else 'blocks'
        blocks = db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        free_pages = db.execute('PRAGMA freelist_count').fetchone()[0]
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
    finally:
        db.close()
    wal = source.with_name(source.name + '-wal')
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    volume = os.statvfs(source.parent)
    print('write_overlay_snapshot=' + json.dumps({
        'observed_at_unix': time.time(), 'database_bytes': info.st_size,
        'allocated_bytes': info.st_blocks * 512, 'wal_bytes': wal_bytes,
        'changed_blocks': blocks, 'logical_changed_bytes': blocks * 65536,
        'logical_limit_bytes': 2 * 1024**3, 'reusable_page_bytes': free_pages * page_size,
        'volume_available_bytes': volume.f_bavail * volume.f_frsize,
        'codec': 'zlib-v1' if encoded else 'raw', 'read_only': True}, sort_keys=True))
except Exception as error:
    print('write_overlay_snapshot=' + type(error).__name__)
# storage-snapshot:end
try:
    transport = str(pathlib.Path.home() / 'vos-fleet/tools/fleet/fleet_push.py')
    rows = subprocess.check_output(['ps', '-axo', 'pid,command'], text=True, timeout=5)
    security = []
    http_security = []
    for line in rows.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            program = pathlib.Path(parts[1].split(None, 1)[0]).name
            if program in ('curl', 'wget') and 'virtualos-relay.ai-c24.workers.dev' in parts[1]:
                http_security.append({'pid': int(parts[0]), 'program': program,
                    'bearer_header_argument_present': 'authorization:' in parts[1].lower()
                        and 'bearer ' in parts[1].lower()})
        if transport in line and '--watch' in line and ' -c ' not in line and 'ssh ' not in line:
            security.append({'pid': int(line.strip().split(None, 1)[0]),
                'token_argument_present': '--token ' in line,
                'token_file_argument_present': '--token-file ' in line})
    print('transport_security=' + json.dumps(security, sort_keys=True))
    print('relay_http_process_security=' + json.dumps(http_security, sort_keys=True))
except Exception as error:
    print('transport_security=' + type(error).__name__)
try:
    path = pathlib.Path.home() / '.config/virtualos/relay.env'
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError('relay credential directory must remain private')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise ValueError('relay credentials require a private regular file')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as env_file:
        info = os.fstat(env_file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError('relay credential descriptor is unsafe')
        body = env_file.read(4097)
        if len(body) > 4096:
            raise ValueError('relay credentials exceed bound')
    values = {}
    for line in body.decode('utf-8').splitlines():
        key, _, value = line.removeprefix('export ').partition('=')
        if key in ('RELAY_URL', 'AUTH_TOKEN', 'VOS_AUTH_TOKEN'):
            parts = shlex.split(value, comments=True)
            if len(parts) == 1:
                values[key] = parts[0]
    url = values.get('RELAY_URL', '').rstrip('/')
    token = values.get('AUTH_TOKEN') or values.get('VOS_AUTH_TOKEN')
    if url != 'https://virtualos-relay.ai-c24.workers.dev' or not token or len(token) > 4096:
        raise ValueError('relay identity or credential missing')
    relay = get(url + '/fleet/status', token)
    print('relay_liveness=' + json.dumps(relay.get('liveness', {}), sort_keys=True))
except Exception as error:
    print('relay_liveness=' + type(error).__name__)
HOST_STATUS
'''
try:
    result = subprocess.run(ssh + [remote], capture_output=True, text=True, timeout=35)
    print(result.stdout.strip(), flush=True)
    print('mini_access=' + ('ok' if result.returncode == 0 else 'failed'), flush=True)
    if result.returncode:
        print(result.stderr.strip()[:400], flush=True)
        sys.exit(1)
except subprocess.TimeoutExpired:
    print('mini_access=timeout', flush=True)
    sys.exit(1)
except OSError as error:
    print('mini_access=' + type(error).__name__, flush=True)
    sys.exit(1)
PY
cat "$snapshot_dir/observer-next.txt"
mv -f "$snapshot_dir/observer-next.txt" "$snapshot_dir/observer-latest.txt"
sleep 30
done
