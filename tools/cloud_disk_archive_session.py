"""Private, reconnectable signing channel; no R2 keys leave the operator."""
import fcntl
import hashlib
import http.client
import json
import os
import re
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib import error, parse, request

ROOT = Path('/Users/ai/vos-shared-fleet/archive-jobs')
MAX_MESSAGE = 65536
MAX_SESSION_SECONDS = 72 * 3600


class RemoteWorkerError(ValueError):
    def __init__(self, row):
        error_type = row.get('type')
        category = row.get('category')
        if (not isinstance(error_type, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', error_type)
                or category not in ('integrity', 'worker_failure')):
            raise ValueError('invalid remote error metadata')
        self.worker_error_type, self.category = error_type, category
        super().__init__('detached worker failed')


class ArchiveSource:
    """Resume compressed HTTP reads without resetting the gzip parser or hashes."""
    def __init__(self, url, size, *, opener=None, pause=time.sleep, renew=None):
        from cloud_disk_r2 import _NoRedirect
        self.url, self.size, self.position = url, size, 0
        self.opener = opener or request.build_opener(request.ProxyHandler({}), _NoRedirect)
        self.pause = pause
        self.renew = renew
        self.origin = parse.urlsplit(url)
        self.response = None
        self.etag = None

    def open(self):
        remaining = self.size - self.position
        response = self.opener.open(request.Request(self.url,
            headers={'Range': 'bytes=' + str(self.position) + '-', 'Accept-Encoding': 'identity'}), timeout=120)
        try:
            status = response.status
            if (status not in (200, 206) or status == 200 and self.position != 0
                    or response.headers.get('Content-Length') != str(remaining)):
                raise ValueError('archive range response has an unexpected size or status')
            if status == 206 and response.headers.get('Content-Range') != (
                    'bytes %d-%d/%d' % (self.position, self.size - 1, self.size)):
                raise ValueError('archive range response does not match requested offset')
            if response.headers.get('Content-Encoding', 'identity') != 'identity':
                raise ValueError('archive transport changed its encoding')
            etag = response.headers.get('ETag')
            if self.etag is not None and etag != self.etag:
                raise ValueError('archive object identity changed during reconnect')
            self.etag = etag
        except Exception:
            response.close()
            raise
        self.response = response

    def read(self, size):
        if not 0 <= size <= 4 * 1024**2:
            raise ValueError('archive range read exceeds bound')
        if size == 0 or self.position == self.size:
            return b''
        for attempt in range(6):
            try:
                if self.response is None:
                    self.open()
                try:
                    data = self.response.read(min(size, self.size - self.position))
                except http.client.IncompleteRead as failure:
                    data = failure.partial
                    self.close()
                if not data:
                    raise OSError('archive stream ended before its pinned size')
                if not isinstance(data, bytes) or len(data) > min(size, self.size - self.position):
                    raise ValueError('archive range read returned excess data')
                self.position += len(data)
                return data
            except error.HTTPError as failure:
                status = failure.code
                failure.close()
                self.close()
                if status in (401, 403) and self.renew is not None and attempt < 5:
                    replacement = self.renew()
                    candidate = parse.urlsplit(replacement)
                    if (candidate.scheme != 'https' or candidate.username or candidate.password
                            or candidate.fragment or
                            (candidate.netloc, candidate.path) != (self.origin.netloc, self.origin.path)):
                        raise ValueError('renewed archive capability changed pinned source')
                    self.url = replacement
                    continue
                if status not in (408, 429, 500, 502, 503, 504):
                    raise PermissionError('archive access refused with HTTP %d' % status) from None
                if attempt == 5:
                    raise OSError('archive HTTP retries exhausted') from None
                self.pause(min(2 ** attempt, 16))
            except (OSError, http.client.HTTPException):
                self.close()
                if attempt == 5:
                    raise OSError('archive range retries exhausted') from None
                self.pause(min(2 ** attempt, 16))

    def close(self):
        if self.response is not None:
            self.response.close()
        self.response = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SigningSession:
    """Operator-side bridge which reconnects to the same detached Mini worker."""
    def __init__(self, ssh, remote, job, environment, *, disconnected=lambda: None):
        self.command = ssh + ['python3 -u ' + remote + '/cloud_disk_archive_session.py']
        bootstrap = subprocess.run(self.command[:-1] + [self.command[-1] + ' start'],
            input=json.dumps(job) + '\n', text=True, capture_output=True,
            env=environment, check=True, timeout=90)
        self.session = json.loads(bootstrap.stdout)['session']
        if self.session != identity(job):
            raise ValueError('remote session does not match pinned job')
        self.environment = environment
        self.disconnected = disconnected
        self.process = None
        self.reconnects = 0
        self.consecutive_disconnects = 0
        self.deadline = time.monotonic() + MAX_SESSION_SECONDS

    def connect(self):
        self.process = subprocess.Popen(self.command[:-1] + [self.command[-1] + ' bridge ' + self.session],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
            env=self.environment)

    def __iter__(self):
        return self

    def __next__(self):
        while time.monotonic() < self.deadline:
            if self.process is None:
                self.connect()
            line = self.process.stdout.readline(MAX_MESSAGE + 1)
            if line:
                if len(line) > MAX_MESSAGE or not line.endswith('\n'):
                    raise ValueError('remote signing message exceeds bound')
                row = json.loads(line)
                if 'remote_error' in row:
                    raise RemoteWorkerError(row['remote_error'])
                self.consecutive_disconnects = 0
                return line
            self.close()
            self.reconnects += 1
            self.consecutive_disconnects = getattr(self, 'consecutive_disconnects', 0) + 1
            if self.consecutive_disconnects > 120:
                break
            self.disconnected()
            time.sleep(min(5 * self.consecutive_disconnects, 30))
        raise ConnectionError('signer reconnect budget exhausted; detached Mini job retained')

    def send(self, row):
        try:
            self.process.stdin.write(json.dumps(row) + '\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            # The Mini retains and resends this same pending request.
            self.close()

    def close(self):
        if self.process is not None:
            process, self.process = self.process, None
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            try:
                process.stdin.close()
            except OSError:
                pass
            process.stdout.close()


def identity(job):
    pinned = {key: value for key, value in job.items() if key != 'source_url'}
    return hashlib.sha256(json.dumps(pinned, sort_keys=True).encode()).hexdigest()[:24]


def private(path):
    if path.is_symlink():
        raise ValueError('unsafe signing session directory')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('unsafe signing session directory')
    path.chmod(0o700)
    return path


def safe_file(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            raise ValueError('unsafe signing session file')
        path.chmod(0o600)
    return path


def write_json(path, row):
    safe_file(path)
    temporary = safe_file(path.with_name(path.name + '.next'))
    temporary.write_text(json.dumps(row, sort_keys=True) + '\n')
    temporary.replace(path)


def message(stream):
    line = stream.readline(MAX_MESSAGE + 1)
    if not line:
        raise EOFError('signer disconnected')
    if len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
        raise ValueError('signing message exceeds bound')
    return json.loads(line)


class Channel:
    """Keep the pending block in memory while the SSH transport reconnects."""
    def __init__(self, directory, *, deadline=None):
        self.path = directory / 's.sock'
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('unsafe signing socket')
            self.path.unlink()
        self.listener = socket.socket(socket.AF_UNIX)
        try:
            self.listener.bind(str(self.path))
        except Exception:
            self.listener.close()
            raise
        self.path.chmod(0o600)
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.deadline = deadline or time.monotonic() + MAX_SESSION_SECONDS
        self.connection = self.reader = None

    def disconnect(self):
        if self.reader is not None:
            self.reader.close()
        if self.connection is not None:
            self.connection.close()
        self.connection = self.reader = None

    def connect(self):
        while self.connection is None:
            if time.monotonic() >= self.deadline:
                raise TimeoutError('signing session lifetime exceeded')
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            connection.settimeout(60)
            self.connection = connection
            self.reader = connection.makefile('rb')

    def exchange(self, row):
        while True:
            self.connect()
            try:
                self.connection.sendall((json.dumps(row) + '\n').encode())
                reply = message(self.reader)
                if reply.get('id') != row['request']['id']:
                    raise ValueError('signer replied to a different request')
                return reply
            except (OSError, EOFError):
                self.disconnect()

    def phase(self, row):
        if self.connection is not None:
            try:
                self.connection.sendall((json.dumps({'phase': row}) + '\n').encode())
            except OSError:
                self.disconnect()

    def close(self):
        self.disconnect()
        self.listener.close()
        self.path.unlink(missing_ok=True)


def start(job, root=ROOT):
    directory = private(private(root) / identity(job))
    for name in ('start.lock', 'worker.lock', 'job.json', 'result.json', 'error.json', 'worker.log'):
        safe_file(directory / name)
    with (directory / 'start.lock').open('a') as launch:
        fcntl.flock(launch, fcntl.LOCK_EX)
        if (directory / 'result.json').exists():
            return {'session': directory.name, 'state': 'complete'}
        if (directory / 'error.json').exists():
            return {'session': directory.name, 'state': 'failed'}
        with (directory / 'worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'session': directory.name, 'state': 'running'}
            write_json(directory / 'job.json', job)
            # Transfer the already-held lock across exec: no launch/PID race.
            environment = {k: v for k, v in os.environ.items() if not
                (k.startswith(('AWS_', 'CF_', 'CLOUDFLARE_')) or
                 any(word in k.upper() for word in ('SECRET', 'TOKEN', 'PASSWORD', 'API_KEY')))}
            with (directory / 'worker.log').open('a') as log:
                subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()),
                                  'daemon', str(directory), str(lock.fileno())],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True, pass_fds=(lock.fileno(),), env=environment)
            # The child owns the shared open-file description until it exits.
    return {'session': directory.name, 'state': 'started'}


def daemon(directory, lock_fd):
    lock = os.fdopen(lock_fd, 'a')
    channel = None
    try:
        job_path = safe_file(directory / 'job.json')
        job = json.loads(job_path.read_text())
        if identity(job) != directory.name:
            raise ValueError('session identity differs from pinned job')
        job_path.unlink()  # The expiring source capability is needed only in memory.
        os.nice(10)  # Background conversion yields CPU to the live guests.
        channel = Channel(directory)
        from cloud_disk_archive_delegate import worker
        result = worker(job=job, channel=channel)
        write_json(directory / 'result.json', {'result': result})
        channel.phase({'state': 'remote_complete'})
    except Exception as failure:
        # Never serialize source URLs, signed headers or raw exception text.
        write_json(directory / 'error.json', {'remote_error': {
            'type': type(failure).__name__,
            'category': 'integrity' if isinstance(failure, ValueError) else 'worker_failure'}})
        raise
    finally:
        if channel is not None:
            channel.close()
        lock.close()


def bridge(session, root=ROOT):
    if len(session) != 24 or any(c not in '0123456789abcdef' for c in session):
        raise ValueError('invalid signing session')
    directory = private(private(root) / session)
    for _ in range(100):
        for name in ('result.json', 'error.json'):
            path = safe_file(directory / name)
            if path.exists():
                print(path.read_text().strip(), flush=True)
                return
        path = directory / 's.sock'
        if path.exists():
            break
        time.sleep(0.1)
    connection = socket.socket(socket.AF_UNIX)
    connection.connect(str(path))

    def forward():
        try:
            for line in sys.stdin.buffer:
                if len(line) > MAX_MESSAGE:
                    break
                connection.sendall(line)
        finally:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    threading.Thread(target=forward, daemon=True).start()
    try:
        with connection.makefile('rb') as stream:
            while True:
                line = stream.readline(MAX_MESSAGE + 1)
                if not line:
                    break
                if len(line) > MAX_MESSAGE:
                    raise ValueError('signing message exceeds bound')
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        for name in ('result.json', 'error.json'):
            path = safe_file(directory / name)
            if path.exists():
                print(path.read_text().strip(), flush=True)
                return
        raise EOFError('signing session disconnected')
    finally:
        connection.close()


if __name__ == '__main__':
    os.umask(0o077)
    if sys.argv[1] == 'start':
        print(json.dumps(start(message(sys.stdin.buffer))), flush=True)
    elif sys.argv[1] == 'daemon':
        daemon(Path(sys.argv[2]), int(sys.argv[3]))
    elif sys.argv[1] == 'bridge':
        bridge(sys.argv[2])
    else:
        raise SystemExit('invalid signing session mode')
