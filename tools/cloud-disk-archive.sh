#!/usr/bin/env bash
set -euo pipefail
umask 077
script_dir="$(cd "$(dirname "$0")" && pwd)"
mode="${1:-start}"
case "$mode" in
  start)
    python3 "$script_dir/cloud_disk_work_loop.py" control
    python3 "$script_dir/cloud_disk_work_loop.py" start
    python3 "$script_dir/cloud_disk_archive_delegate.py" start
    if ! python3 "$script_dir/linux_lab_work.py" start; then
      echo 'linux_work=requires_attention' >&2
    fi
    exec bash "$0" watch-start
    ;;
  status) exec python3 "$script_dir/cloud_disk_archive_delegate.py" status ;;
  watch-start|watch)
    if ! python3 "$script_dir/cloud_disk_work_loop.py" recovery-admission; then
      echo 'recovery_watcher=waiting_for_control_gate'
      exit 0
    fi
    exec python3 -u - "$script_dir" "$mode" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

tools = Path(sys.argv[1])
sys.path.insert(0, str(tools))
from cloud_disk_archive_delegate import paths

os.umask(0o077)
directory = paths()
for name in ('recovery-watch.lock', 'recovery-watch-launch.lock', 'recovery-watch.log',
             'recovery-watch.json', 'recovery-watch.json.next'):
    path = directory / name
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise SystemExit('unsafe recovery watcher state file')
        path.chmod(0o600)

def state(value, **fields):
    row = dict(state=value, updated_at=time.time(), pid=os.getpid(), **fields)
    temporary = directory / 'recovery-watch.json.next'
    temporary.write_text(json.dumps(row, sort_keys=True) + '\n')
    temporary.replace(directory / 'recovery-watch.json')
    print(json.dumps(row, sort_keys=True), flush=True)

if sys.argv[2] == 'watch-start':
    with (directory / 'recovery-watch-launch.lock').open('a') as launch:
        fcntl.flock(launch, fcntl.LOCK_EX)
        with (directory / 'recovery-watch.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('recovery_watcher=running')
                raise SystemExit(0)
        environment = {k: v for k, v in os.environ.items() if not
            (k.startswith(('AWS_', 'CF_', 'CLOUDFLARE_')) or
             any(word in k.upper() for word in ('SECRET', 'TOKEN', 'PASSWORD')))}
        with (directory / 'recovery-watch.log').open('a') as log:
            child = subprocess.Popen(['bash', str(tools / 'cloud-disk-archive.sh'), 'watch'],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=environment,
                start_new_session=True)
        print('recovery_watcher_started=' + str(child.pid))
    raise SystemExit(0)

with (directory / 'recovery-watch.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
    try:
        state('waiting_for_conversion')
        while not (directory / 'result.json').exists():
            status_file = directory / 'status.json'
            if status_file.exists():
                try:
                    current = json.loads(status_file.read_text())
                except ValueError:
                    current = {}
                if current.get('state') == 'failed':
                    state('blocked_conversion_failed')
                    raise SystemExit(1)
            time.sleep(30)
        paths()
        result = json.loads((directory / 'result.json').read_text())
        archive = json.loads((tools / 'experiments/r2-vm-archive-receipt.json').read_text())
        base = json.loads((tools / 'experiments/r2-cold-machine-catalog.json').read_text())['base']
        if (result.get('archive_sha256') != archive['sha256'] or
            result.get('archive_bytes') != archive['bytes'] or
            result.get('disk_sha256') != base['sha256'] or
            result.get('logical_bytes') != base['bytes'] or
            result.get('readback_verified') is not True):
            raise ValueError('conversion result does not match pinned archive and base')
        while True:
            admission = subprocess.run([sys.executable, str(tools / 'cloud_disk_work_loop.py'),
                                        'recovery-admission'], check=False)
            if admission.returncode:
                state('waiting_for_control_gate')
                time.sleep(60)
                continue
            with (directory / 'lock').open('a') as active:
                try:
                    fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(1)
                    continue
            state('verifying_cloud_recovery')
            completed = subprocess.run(['bash', str(tools / 'cloud-disk-archive.sh'), 'recover'])
            if completed.returncode == 3:
                admission = subprocess.run([sys.executable, str(tools / 'cloud_disk_work_loop.py'),
                                            'recovery-admission'], check=False)
                if admission.returncode:
                    state('waiting_for_control_gate')
                    time.sleep(60)
                    continue
                recovery = json.loads((directory / 'recovery-result.json').read_text())
                reason = recovery.get('state')
                if reason not in ('waiting_for_capacity', 'waiting_for_resources', 'waiting_for_control_gate'):
                    raise ValueError('recovery retry did not identify a safe wait condition')
                state(reason)
                time.sleep(60)
                continue
            state('recovery_finished' if completed.returncode == 0 else 'recovery_requires_attention',
                  returncode=completed.returncode)
            break
    except Exception as failure:
        state('failed', error=type(failure).__name__)
        raise SystemExit(1)
PY
    ;;
  run|recover)
    set +x
    if [[ "$mode" == recover ]]; then
      python3 "$script_dir/cloud_disk_work_loop.py" recovery-admission || exit "$?"
    fi
    credential_file="${VOS_R2_CREDENTIAL_FILE:-/Users/theone/Documents/GitHub/terraform-ai/cloudflare-credentials.local/terraform-ai-ai-r2-migration-2026-08-24.env}"
    exec python3 -u "$script_dir/cloud_disk_credentials.py" "$credential_file" "$mode"
    ;;
  *) echo 'usage: tools/cloud-disk-archive.sh' >&2; exit 2 ;;
esac

# virtualos-agent-control:begin
: <<'VIRTUALOS_AGENT_CONTROL_PY'
"""Durable work coordination; state describes work and never contains executable commands."""
import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

SCHEMA = 'virtualos-agent-control/v1'
MAX_STATE = 1024 * 1024
MAX_EVIDENCE = 16 * 1024 * 1024
MAX_LEASE = 900


class ControlError(ValueError):
    pass


class LeaseError(ControlError):
    pass


def _clock(now):
    value = time.time() if now is None else now
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ControlError('invalid clock')
    return value


def _label(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:@-]{1,128}', value):
        raise ControlError('invalid work identifier')
    return value


def _directory(path, private=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ControlError('literal absolute directory required')
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise PermissionError('linked directory refused')
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & (0o077 if private else 0o022):
        raise PermissionError('directory must be owned and safely permissioned')
    return path


def _file_info(info, private):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or private and stat.S_IMODE(info.st_mode) != 0o600):
        raise PermissionError('unsafe control file')


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ControlError('duplicate state field')
        value[key] = item
    return value


class Control:
    def __init__(self, directory, repo):
        self.directory = Path(directory)
        self.repo = _directory(repo)
        # Construction is read-only. Mutating methods create the private leaf
        # only when its existing parent and literal path are safe.
        if self.directory.exists() or self.directory.is_symlink():
            _directory(self.directory, private=True)
        else:
            _directory(self.directory.parent)
            if not self.directory.is_absolute() or '..' in self.directory.parts:
                raise ControlError('literal absolute directory required')

    @staticmethod
    def _empty():
        return dict(schema=SCHEMA, revision=0, tasks=[], leases={}, events=[])

    def _read(self):
        if not self.directory.exists():
            return self._empty()
        _directory(self.directory, private=True)
        folder = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                fd = os.open('agent-control.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            except FileNotFoundError:
                return self._empty()
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno()); _file_info(before, True)
                if before.st_size > MAX_STATE:
                    raise ControlError('state exceeds bound')
                body = stream.read(before.st_size + 1)
                after = os.fstat(stream.fileno())
                if len(body) != before.st_size or self._identity(before) != self._identity(after):
                    raise ControlError('state changed during read')
            state = json.loads(body, object_pairs_hook=_pairs)
            self._validate(state)
            return state
        finally:
            os.close(folder)

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    @staticmethod
    def _validate(state):
        if (type(state) is not dict or set(state) != {'schema', 'revision', 'tasks', 'leases', 'events'}
                or state['schema'] != SCHEMA or type(state['revision']) is not int or state['revision'] < 0
                or type(state['tasks']) is not list or len(state['tasks']) > 128
                or type(state['leases']) is not dict or not set(state['leases']) <= {'worker', 'observer'}
                or type(state['events']) is not list or len(state['events']) > 256):
            raise ControlError('invalid control state')
        ids = set()
        for task in state['tasks']:
            required = {'id', 'title', 'acceptance', 'role', 'dependencies', 'approval_required', 'approved', 'resources_ok',
                        'manual_block', 'status', 'evidence', 'last_progress_at'}
            if type(task) is not dict or set(task) != required:
                raise ControlError('invalid task fields')
            name = _label(task['id'])
            if name in ids or task['role'] not in ('worker', 'observer'):
                raise ControlError('invalid task role or duplicate')
            ids.add(name)
            if any(not isinstance(task[key], str) or len(task[key]) > 2048
                   or any(ord(char) < 32 for char in task[key]) for key in ('title', 'acceptance')):
                raise ControlError('invalid task instructions')
            if (type(task['dependencies']) is not list or len(task['dependencies']) > 128
                    or len(set(task['dependencies'])) != len(task['dependencies'])
                    or any(type(task[key]) is not bool for key in ('approval_required', 'approved', 'resources_ok'))
                    or task['status'] not in ('pending', 'active', 'blocked', 'done')
                    or task['manual_block'] is not None and task['manual_block'] not in ('error', 'evidence_invalid')):
                raise ControlError('invalid task policy')
            for dependency in task['dependencies']:
                _label(dependency)
            if task['last_progress_at'] is not None:
                _clock(task['last_progress_at'])
            evidence = task['evidence']
            if evidence is not None:
                if (type(evidence) is not dict or set(evidence) != {'path', 'sha256', 'bindings', 'attested_by', 'attested_at', 'lease_token'}
                        or evidence['attested_by'] != 'observer' or type(evidence['path']) is not str
                        or type(evidence['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', evidence['sha256'])
                        or type(evidence['bindings']) is not dict or len(evidence['bindings']) > 128):
                    raise ControlError('invalid evidence binding')
                if evidence['lease_token'] is not None and (type(evidence['lease_token']) is not str
                        or not re.fullmatch('[0-9a-f]{32}', evidence['lease_token'])):
                    raise ControlError('invalid evidence attempt')
                for relative, pin in evidence['bindings'].items():
                    if (not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts
                            or not relative or type(pin) is not str or not re.fullmatch('[0-9a-f]{64}', pin)):
                        raise ControlError('invalid source binding')
                _clock(evidence['attested_at'])
        tasks = {task['id']: task for task in state['tasks']}
        visiting, visited = set(), set()
        def visit(name):
            if name in visiting or name not in tasks:
                raise ControlError('cyclic or missing dependency')
            if name in visited:
                return
            visiting.add(name)
            for dependency in tasks[name]['dependencies']:
                visit(dependency)
            visiting.remove(name); visited.add(name)
        for name in tasks:
            visit(name)
        active = set()
        for role, lease in state['leases'].items():
            if (type(lease) is not dict or set(lease) != {'token', 'task', 'owner', 'role', 'issued_at', 'expires_at'}
                    or lease['role'] != role or not isinstance(lease['token'], str)
                    or not re.fullmatch('[0-9a-f]{32}', lease['token'])):
                raise ControlError('invalid lease')
            _label(lease['owner']); _label(lease['task'])
            start, end = _clock(lease['issued_at']), _clock(lease['expires_at'])
            if (not 0 < end - start <= MAX_LEASE or lease['task'] not in tasks
                    or tasks[lease['task']]['role'] != role or tasks[lease['task']]['status'] != 'active'):
                raise ControlError('invalid lease bounds')
            active.add(lease['task'])
        if active != {task['id'] for task in tasks.values() if task['status'] == 'active'}:
            raise ControlError('active work lacks an exclusive lease')
        for event in state['events']:
            if (type(event) is not dict or set(event) != {'at', 'kind', 'task', 'detail'}
                    or not isinstance(event['detail'], str) or len(event['detail']) > 512):
                raise ControlError('invalid event')
            _clock(event['at']); _label(event['kind'])
            if event['task'] is not None:
                _label(event['task'])

    @contextlib.contextmanager
    def _transaction(self):
        if not self.directory.exists():
            self.directory.mkdir(mode=0o700, exist_ok=True)
        _directory(self.directory, private=True)
        folder = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock = None
        temporary = None
        try:
            lock = os.open('agent-control.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=folder)
            _file_info(os.fstat(lock), True)
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('control lock busy')
                    time.sleep(0.01)
            state = self._read()
            yield state
            state['revision'] += 1
            self._validate(state)
            body = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            if len(body) > MAX_STATE:
                raise ControlError('state exceeds bound')
            temporary = '.agent-control-' + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, 'agent-control.json', src_dir_fd=folder, dst_dir_fd=folder)
            temporary = None
            os.fsync(folder)
        finally:
            if temporary is not None:
                os.unlink(temporary, dir_fd=folder)
            if lock is not None:
                os.close(lock)
            os.close(folder)

    @staticmethod
    def _event(state, now, kind, task=None, detail=''):
        state['events'].append(dict(at=now, kind=kind, task=task, detail=detail[:512]))
        del state['events'][:-256]

    @staticmethod
    def _task(state, name):
        for task in state['tasks']:
            if task['id'] == name:
                return task
        raise ControlError('unknown task')

    @staticmethod
    def _blocks(state, task):
        tasks = {item['id']: item for item in state['tasks']}
        reasons = ['dependency:' + name for name in task['dependencies'] if tasks[name]['status'] != 'done']
        if task['approval_required'] and not task['approved']:
            reasons.append('approval')
        if not task['resources_ok']:
            reasons.append('resources')
        if task['manual_block']:
            reasons.append(task['manual_block'])
        return reasons

    def _evidence_hash(self, value):
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts:
            raise ControlError('literal absolute evidence path required')
        allowed = next((base for base in (self.repo, self.directory) if base in path.parents), None)
        if allowed is None:
            raise ControlError('evidence must be inside repo or private job directory')
        _directory(allowed, private=allowed == self.directory)
        folder = os.open(allowed, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = path.relative_to(allowed).parts
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
                os.close(folder); folder = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno()); _file_info(before, False)
                if before.st_size > MAX_EVIDENCE:
                    raise ControlError('evidence exceeds bound')
                remaining, result = before.st_size, hashlib.sha256()
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ControlError('evidence truncated')
                    remaining -= len(chunk); result.update(chunk)
                if stream.read(1) or self._identity(before) != self._identity(os.fstat(stream.fileno())):
                    raise ControlError('evidence changed during read')
                return result.hexdigest()
        finally:
            os.close(folder)

    def _valid_evidence(self, task):
        evidence = task['evidence']
        if evidence is None:
            return False
        try:
            return (self._evidence_hash(evidence['path']) == evidence['sha256']
                    and all(self._evidence_hash(self.repo / relative) == pin
                            for relative, pin in evidence['bindings'].items()))
        except (OSError, ControlError):
            return False

    def _reconcile(self, state, now):
        for role, lease in list(state['leases'].items()):
            if now >= lease['expires_at'] or now < lease['issued_at']:
                self._task(state, lease['task'])['status'] = 'pending'
                del state['leases'][role]
                self._event(state, now, 'lease_expired', lease['task'])
        for task in state['tasks']:
            if task['status'] == 'done' and not self._valid_evidence(task):
                task['status'], task['manual_block'] = 'blocked', 'evidence_invalid'
                self._event(state, now, 'evidence_invalid', task['id'])
        changed = True
        while changed:
            changed = False
            for task in state['tasks']:
                if task['status'] == 'done' and self._blocks(state, task):
                    task['status'] = 'blocked'
                    self._event(state, now, 'prerequisite_invalid', task['id'])
                    changed = True
        for role, lease in list(state['leases'].items()):
            task = self._task(state, lease['task'])
            if self._blocks(state, task):
                task['status'] = 'blocked'
                del state['leases'][role]
                self._event(state, now, 'lease_blocked', task['id'])
        for task in state['tasks']:
            if task['status'] not in ('active', 'done'):
                task['status'] = 'blocked' if self._blocks(state, task) else 'pending'

    def snapshot(self, now=None):
        now = _clock(now)
        state = self._read()
        view = copy.deepcopy(state)
        self._reconcile(view, now)
        # Derived status is truthful, while persisted events/revision remain a
        # read-only history: observation never invents a durable transition.
        view['events'] = state['events']
        view['snapshot_at'] = now
        view['stale_leases'] = [lease['token'] for lease in state['leases'].values() if now >= lease['expires_at']]
        for task in view['tasks']:
            task['block_reasons'] = self._blocks(view, task)
            task['evidence_valid'] = self._valid_evidence(task)
        return view

    def seed(self, tasks, now=None):
        now = _clock(now)
        with self._transaction() as state:
            existing = {task['id'] for task in state['tasks']}
            for definition in tasks:
                if set(definition) - {'id', 'title', 'acceptance', 'role', 'dependencies', 'approval_required', 'resources_ok'}:
                    raise ControlError('unknown task definition field')
                name = _label(definition['id'])
                if name in existing:
                    continue
                state['tasks'].append(dict(id=name, title=definition.get('title', name),
                    acceptance=definition.get('acceptance', 'Observer-attested evidence and exact source hashes required.'),
                    role=definition.get('role', 'worker'),
                    dependencies=definition.get('dependencies', []), approval_required=definition.get('approval_required', False),
                    approved=False, resources_ok=definition.get('resources_ok', True), manual_block=None,
                    status='pending', evidence=None, last_progress_at=None))
                existing.add(name)
                self._event(state, now, 'task_seeded', name)
            self._validate(state)
            self._reconcile(state, now)
        return self.snapshot(now)

    def reconcile(self, now=None):
        now = _clock(now)
        with self._transaction() as state:
            self._reconcile(state, now)
        return self.snapshot(now)

    def claim(self, owner, now=None, role='worker', ttl=120):
        owner, now = _label(owner), _clock(now)
        if role not in ('worker', 'observer') or type(ttl) is not int or not 1 <= ttl <= MAX_LEASE:
            raise ControlError('invalid lease policy')
        answer = None
        with self._transaction() as state:
            self._reconcile(state, now)
            if role not in state['leases']:
                for task in state['tasks']:
                    if task['role'] == role and task['status'] == 'pending' and not self._blocks(state, task):
                        answer = dict(token=uuid.uuid4().hex, task=task['id'], owner=owner, role=role,
                                      issued_at=now, expires_at=now + ttl)
                        state['leases'][role] = answer
                        task['status'], task['last_progress_at'] = 'active', now
                        self._event(state, now, 'claimed', task['id'], owner)
                        break
        return copy.deepcopy(answer)

    def _lease(self, state, lease, now):
        if type(lease) is not dict or lease.get('role') not in state['leases']:
            raise LeaseError('lease unavailable')
        live = state['leases'][lease['role']]
        if live != lease or now >= live['expires_at'] or now < live['issued_at']:
            raise LeaseError('lease stale or ownership mismatch')
        return self._task(state, live['task'])

    def heartbeat(self, lease, now=None, ttl=120):
        now = _clock(now)
        if type(ttl) is not int or not 1 <= ttl <= MAX_LEASE:
            raise ControlError('invalid lease duration')
        with self._transaction() as state:
            self._reconcile(state, now)
            task = self._lease(state, lease, now)
            live = state['leases'][lease['role']]
            live['issued_at'], live['expires_at'] = now, now + ttl
            task['last_progress_at'] = now
            answer = copy.deepcopy(live)
            self._event(state, now, 'heartbeat', task['id'])
        return answer

    def progress(self, lease, message, now=None):
        now = _clock(now)
        if not isinstance(message, str) or len(message) > 512 or any(ord(c) < 32 for c in message):
            raise ControlError('invalid progress detail')
        with self._transaction() as state:
            task = self._lease(state, lease, now)
            task['last_progress_at'] = now
            self._event(state, now, 'progress', task['id'], message)

    def block(self, lease, reason, now=None):
        now = _clock(now)
        if reason not in ('resources', 'approval', 'error'):
            raise ControlError('invalid block reason')
        with self._transaction() as state:
            task = self._lease(state, lease, now)
            if reason == 'resources':
                task['resources_ok'] = False
            elif reason == 'approval':
                task['approval_required'], task['approved'] = True, False
            else:
                task['manual_block'] = 'error'
            task['status'] = 'blocked'
            del state['leases'][lease['role']]
            self._event(state, now, 'blocked', task['id'], reason)

    def set_gate(self, task, gate, value, actor='observer', now=None):
        now = _clock(now)
        if actor != 'observer' or gate not in ('approval', 'resources', 'error') or type(value) is not bool:
            raise ControlError('explicit observer gate decision required')
        with self._transaction() as state:
            item = self._task(state, task)
            if gate == 'approval':
                item['approved'] = value
            elif gate == 'resources':
                item['resources_ok'] = value
            else:
                item['manual_block'] = 'error' if value else None
            self._event(state, now, 'gate_changed', task, gate + ':' + str(value))
            self._reconcile(state, now)

    def attest(self, task, evidence_path, sha256, actor='observer', complete=False, now=None, bindings=None):
        now = _clock(now)
        if actor != 'observer' or not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
            raise ControlError('observer and exact evidence digest required')
        path = str(Path(evidence_path))
        with self._transaction() as state:
            item = self._task(state, task)
            if self._evidence_hash(path) != sha256:
                raise ControlError('evidence digest mismatch')
            if bindings is None:
                bindings = {}
            if type(bindings) is not dict:
                raise ControlError('source binding map required')
            for relative, pin in bindings.items():
                relative_path = Path(relative)
                if (relative_path.is_absolute() or '..' in relative_path.parts
                        or self._evidence_hash(self.repo / relative_path) != pin):
                    raise ControlError('source binding mismatch')
            item['evidence'] = dict(path=path, sha256=sha256, bindings=copy.deepcopy(bindings),
                                    attested_by=actor, attested_at=now,
                                    lease_token=next((lease['token'] for lease in state['leases'].values()
                                                      if lease['task'] == task), None))
            if item['manual_block'] == 'evidence_invalid':
                item['manual_block'] = None
            self._reconcile(state, now)
            if complete:
                if item['status'] == 'active' or self._blocks(state, item):
                    raise ControlError('cannot import completion of active or blocked work')
                item['status'] = 'done'
            self._event(state, now, 'evidence_attested', task, sha256)

    def complete(self, lease, now=None):
        now = _clock(now)
        with self._transaction() as state:
            self._reconcile(state, now)
            task = self._lease(state, lease, now)
            if (self._blocks(state, task) or not self._valid_evidence(task)
                    or task['evidence']['lease_token'] != lease['token']):
                raise ControlError('completion requires eligible work and unchanged observer evidence')
            task['status'] = 'done'
            del state['leases'][lease['role']]
            self._event(state, now, 'completed', task['id'], task['evidence']['sha256'])

VIRTUALOS_AGENT_CONTROL_PY
# virtualos-agent-control:end
