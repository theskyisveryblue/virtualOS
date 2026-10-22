"""Stream the existing pinned archive on the Mini; keep S3 keys on operator."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib import request

from cloud_disk import CHUNK, MAX_MANIFEST_BYTES, digest, validate_config
from cloud_disk_archive import stream_archive_upload
from cloud_disk_r2_upload import R2RetryableError, R2UploadStore


def retry_r2(operation, *, phase=lambda row: None, pause=time.sleep, attempts=60):
    if type(attempts) is not int or not 1 <= attempts <= 60:
        raise ValueError('invalid R2 retry budget')
    for attempt in range(attempts):
        try:
            return operation()
        except R2RetryableError:
            if attempt == attempts - 1:
                raise
            delay = min(2 ** min(attempt, 5), 30)
            phase(dict(state='retrying_r2', attempt=attempt + 1,
                       retry_budget=attempts, wait_seconds=delay))
            pause(delay)


def conversion_generation(directory, *, advance=False):
    from cloud_disk_archive_session import write_json
    path = directory / 'conversion-attempt.json'
    value = json.loads(path.read_text())['generation'] if path.exists() else 0
    if type(value) is not int or not 0 <= value < 1000000:
        raise ValueError('invalid conversion generation')
    if advance:
        value += 1
        write_json(path, {'generation': value})
    return value


def emit(row):
    print(json.dumps(row, sort_keys=True), flush=True)


def save_status(directory, state):
    temporary = directory / 'status.json.next'
    temporary.write_text(json.dumps(state, sort_keys=True) + '\n')
    temporary.replace(directory / 'status.json')


def worker(job=None, channel=None):
    job = json.loads(sys.stdin.readline()) if job is None else job
    config = job['config']
    os.environ.update(CLOUDFLARE_ACCOUNT_ID=config['account_id'],
                      CLOUDFLARE_R2_ENDPOINT=config['endpoint'],
                      AWS_ACCESS_KEY_ID='delegated', AWS_SECRET_ACCESS_KEY='delegated')
    os.environ.pop('AWS_SESSION_TOKEN', None)
    sequence = 0

    def transport_once(unsigned, limit):
        nonlocal sequence
        sequence += 1
        prefix = config['endpoint'] + '/' + config['bucket'] + '/' + config['object_prefix'] + '/'
        if not unsigned.full_url.startswith(prefix):
            raise ValueError('request escaped pinned destination')
        row = {'request': dict(id=sequence, method=unsigned.get_method(),
                             key=unsigned.full_url[len(prefix):], length=len(unsigned.data or b''),
                             sha256=unsigned.get_header('X-amz-content-sha256'))}
        if channel is not None:
            reply = channel.exchange(row)
        else:
            emit(row)
            reply = json.loads(sys.stdin.readline())
        if reply.get('id') != sequence:
            raise ValueError('unexpected signer response')
        actual = request.Request(unsigned.full_url, data=unsigned.data,
                                 headers=reply['headers'], method=unsigned.get_method())
        return R2UploadStore._persistent_network(actual, limit)

    def transport(unsigned, limit):
        # Keep this exact bounded chunk and both stream hashes alive through
        # transient R2 failures. Every retry obtains a fresh scoped signature;
        # a PUT whose response was lost remains immutable and read-back checked.
        return retry_r2(lambda: transport_once(unsigned, limit),
                        phase=channel.phase if channel else lambda row: emit({'phase': row}))

    store = R2UploadStore(config, transport=transport)
    if job.get('operation') == 'recover':
        from cloud_disk_recovery import recover
        result = recover(store, config, job['manifest_sha256'], job['pins'],
                         progress=channel.phase if channel else lambda phase: emit({'phase': phase}))
        if channel is None:
            emit({'result': result})
        return result
    # The source URL is private, scoped to one immutable object, and never logged.
    from cloud_disk_archive_session import ArchiveSource
    def renew_source():
        nonlocal sequence
        sequence += 1
        row = {'request': dict(id=sequence, method='SOURCE', key='archive', length=0, sha256=digest(b''))}
        if channel is not None:
            reply = channel.exchange(row)
        else:
            emit(row)
            reply = json.loads(sys.stdin.readline())
        if reply.get('id') != sequence or not isinstance(reply.get('source_url'), str):
            raise ValueError('invalid renewed archive capability')
        return reply['source_url']
    with ArchiveSource(job['source_url'], job['pins']['archive_bytes'], renew=renew_source) as source:
        result = stream_archive_upload(source, store, **job['pins'],
            progress=channel.phase if channel else lambda row: emit({'phase': row}))
    if channel is None:
        emit({'result': result})
    return result


def paths():
    directory = Path(os.environ.get('VOS_ARCHIVE_JOB_DIR', '/private/tmp/virtualos-archive-conversion'))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('job directory must be a real directory owned by this user')
    directory.chmod(0o700)
    for name in ('pid', 'status.json', 'status.json.next', 'result.json', 'run.log', 'lock', 'launch.lock',
                 'recovery.json', 'recovery-result.json', 'result.json.next',
                 'conversion-attempt.json', 'conversion-attempt.json.next'):
        target = directory / name
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError('job files must be regular files owned by this user')
            target.chmod(0o600)
    return directory


def status():
    directory = paths()
    result = {'job_directory': str(directory)}
    for filename in ('pid', 'status.json', 'result.json'):
        target = directory / filename
        if target.exists():
            data = target.read_text()
            result[filename] = json.loads(data) if filename.endswith('.json') else int(data)
    if 'pid' in result:
        try:
            os.kill(result['pid'], 0)
            result['process_alive'] = True
        except ProcessLookupError:
            result['process_alive'] = False
        except PermissionError:
            result['process_alive'] = None
            result['process_check'] = 'permission_denied'
    emit(result)


def operator(operation='convert'):
    directory = paths()
    # Exclusive lock rejects two operators writing to the same immutable target.
    import fcntl
    with (directory / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (directory / 'pid').write_text(str(os.getpid()))
        save_status(directory, dict(state='starting_recovery' if operation == 'recover' else 'starting_conversion',
                                   started_at=time.time()))
        try:
            return run_conversion(directory, operation)
        except Exception as failure:
            try:
                current = json.loads((directory / 'status.json').read_text())
            except (OSError, ValueError):
                current = {}
            if current.get('state') != 'failed':
                retryable = isinstance(failure, subprocess.TimeoutExpired) or (
                    isinstance(failure, subprocess.CalledProcessError) and failure.returncode == 255)
                current.update(state='failed', error=type(failure).__name__, updated_at=time.time(),
                               retryable_connection_failure=retryable)
                save_status(directory, current)
            raise


def run_conversion(directory, operation='convert'):
    tools = Path(__file__).resolve().parent
    archive = json.loads((tools / 'experiments/r2-vm-archive-receipt.json').read_text())
    base = json.loads((tools / 'experiments/r2-cold-machine-catalog.json').read_text())['base']
    if any(base['archive_' + field] != archive[field] for field in ('bucket', 'key', 'bytes', 'sha256')):
        raise ValueError('archive receipt and base catalog differ')
    config = validate_config(tools / 'cloudflare/cloud-disk.ai.json')
    config['object_prefix'] = 'disks/archive-base-' + archive['sha256'][:16]
    signer = R2UploadStore(config)
    pins = dict(archive_bytes=archive['bytes'], archive_sha256=archive['sha256'],
                disk_bytes=base['bytes'], disk_sha256=base['sha256'])
    job = dict(config=config, pins=pins, operation=operation)
    if operation == 'recover':
        converted = json.loads((directory / 'result.json').read_text())
        expected_conversion = dict(archive_bytes=pins['archive_bytes'], archive_sha256=pins['archive_sha256'],
                                   logical_bytes=pins['disk_bytes'], disk_sha256=pins['disk_sha256'])
        if (converted['config'] != config or any(converted[key] != value for key, value in expected_conversion.items())
                or converted.get('readback_verified') is not True
                or not re.fullmatch(r'[0-9a-f]{64}', converted['manifest_sha256'])):
            raise ValueError('conversion result does not match pinned recovery source')
        job['manifest_sha256'] = converted['manifest_sha256']
        previous_path = directory / 'recovery-result.json'
        job['recovery_generation'] = 0
        if previous_path.exists():
            previous = json.loads(previous_path.read_text())
            if (previous.get('manifest_sha256') == converted['manifest_sha256']
                    and previous.get('disk_sha256') == pins['disk_sha256']
                    and previous.get('logical_bytes') == pins['disk_bytes']
                    and previous.get('state') in ('frames_ready_for_visual_review', 'verified')):
                emit({'result': previous})
                return previous
            job['recovery_generation'] = previous.get('completed_at', 0)
    else:
        job['conversion_generation'] = conversion_generation(directory)
        job['source_url'] = subprocess.check_output(['aws', 's3', 'presign',
            's3://' + archive['bucket'] + '/' + archive['key'], '--expires-in', '3600',
            '--endpoint-url', config['endpoint']], text=True, timeout=60).strip()
    ssh_config = str(Path.home() / 'Documents/GitHub/realestate-machine/.local/mini-remote-20260915/ssh_config')
    ssh = ['ssh', '-F', ssh_config, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
           '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
    from cloud_disk_connections import control_options
    shared_connection = control_options(directory)
    ssh += shared_connection + ['mac-mini-cloudflare']
    remote = '/Users/ai/vos-shared-fleet/archive-converter'
    subprocess.run(ssh + ['umask 077; if test -L ' + remote + '; then exit 1; fi; mkdir -p ' + remote + '; chmod 700 ' + remote], check=True, timeout=60)
    files = ['cloud_disk.py', 'cloud_disk_r2.py', 'cloud_disk_r2_upload.py',
             'cloud_disk_archive.py', 'cloud_disk_archive_delegate.py', 'cloud_disk_recovery.py',
             'cloud_disk_archive_session.py']
    subprocess.run(['scp', '-F', ssh_config, '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes'] + shared_connection + [str(tools / name) for name in files] +
                   ['mac-mini-cloudflare:' + remote + '/'], check=True, timeout=120)
    environment = {k: v for k, v in os.environ.items() if not
        (k.startswith(('AWS_', 'CF_', 'CLOUDFLARE_')) or any(word in k.upper() for word in ('SECRET', 'TOKEN', 'PASSWORD', 'API_KEY')))}
    state = dict(state='recovering' if operation == 'recover' else 'converting', started_at=time.time(), config=config, pins=pins,
                 signed_requests=0, requested_upload_bytes=0)
    save_status(directory, state)
    result = None
    from cloud_disk_archive_session import RemoteWorkerError, SigningSession
    def disconnected():
        state.update(state='waiting_for_ssh', updated_at=time.time())
        save_status(directory, state)
        emit({'phase': {'state': 'waiting_for_ssh'}})
    session = SigningSession(ssh, remote, job, environment, disconnected=disconnected)
    try:
        for line in session:
            state['state'] = 'recovering' if operation == 'recover' else 'converting'
            row = json.loads(line)
            if set(row) == {'result'}:
                result = row['result']
                break
            if set(row) == {'phase'}:
                state.update(phase=row['phase'], updated_at=time.time())
                save_status(directory, state)
                emit(row)
                continue
            if set(row) != {'request'}:
                raise ValueError('unexpected delegated message')
            item = row['request']
            key, method, length, sha = item['key'], item['method'], item['length'], item['sha256']
            if method == 'SOURCE':
                if (operation == 'recover' or key != 'archive' or type(item['id']) is not int
                        or type(length) is not int or length != 0 or sha != digest(b'')):
                    raise ValueError('invalid archive capability renewal')
                source_url = subprocess.check_output(['aws', 's3', 'presign',
                    's3://' + archive['bucket'] + '/' + archive['key'], '--expires-in', '3600',
                    '--endpoint-url', config['endpoint']], text=True, timeout=60).strip()
                session.send(dict(id=item['id'], source_url=source_url))
                continue
            if not re.fullmatch(r'(?:manifest\.json|objects/[0-9a-f]{64}\.z)', key):
                raise ValueError('invalid delegated object key')
            if type(item['id']) is not int or type(length) is not int or not re.fullmatch(r'[0-9a-f]{64}', sha):
                raise ValueError('invalid delegated request metadata')
            if method == 'GET':
                if length != 0 or sha != digest(b''):
                    raise ValueError('invalid delegated GET')
                extra = None
            elif method == 'PUT':
                if operation == 'recover':
                    raise ValueError('recovery signer permits GET only')
                limit = MAX_MANIFEST_BYTES if key == 'manifest.json' else CHUNK + 65536
                if not 1 <= length <= limit or (key.startswith('objects/') and key[8:-2] != sha):
                    raise ValueError('invalid delegated PUT')
                extra = {'if-none-match': '*'}
                state['requested_upload_bytes'] += length
            else:
                raise ValueError('invalid delegated method')
            signed = signer._request(method, key, extra=extra, payload_sha256=sha)
            session.send(dict(id=item['id'], headers=dict(signed.header_items())))
            state['signed_requests'] += 1
            if state['signed_requests'] % 64 == 0:
                state['updated_at'] = time.time()
                save_status(directory, state)
                emit({'progress': state})
        if result is None:
            raise RuntimeError('remote archive conversion did not complete')
        result.update(config=config, completed_at=time.time(), signing='operator_only')
        result_path = directory / ('recovery-result.json' if operation == 'recover' else 'result.json')
        temporary = result_path.with_name(result_path.name + '.next')
        temporary.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
        temporary.replace(result_path)
        state['state'] = result.get('state', 'complete') if operation == 'recover' else 'complete'
        emit({'result': result})
        return result
    except Exception as exc:
        state.update(state='failed', error=type(exc).__name__)
        if isinstance(exc, ConnectionError):
            state['retryable_connection_failure'] = True
        if isinstance(exc, RemoteWorkerError):
            state.update(remote_worker_failed=True, remote_error_type=exc.worker_error_type,
                         remote_error_category=exc.category)
        raise
    finally:
        state['updated_at'] = time.time()
        save_status(directory, state)
        if operation == 'recover':
            (directory / 'recovery.json').write_text(json.dumps(state, sort_keys=True) + '\n')
        session.close()


if __name__ == '__main__':
    os.umask(0o077)
    mode = sys.argv[1] if len(sys.argv) == 2 else 'status'
    if mode == 'worker':
        worker()
    elif mode == 'run':
        operator()
    elif mode == 'recover':
        recovered = operator('recover')
        if recovered.get('state') in ('waiting_for_capacity', 'waiting_for_resources'):
            raise SystemExit(3)
    elif mode == 'status':
        status()
    elif mode == 'start':
        directory = paths()
        import fcntl
        with (directory / 'launch.lock').open('w') as launch:
            fcntl.flock(launch, fcntl.LOCK_EX)
            active = False
            with (directory / 'lock').open('a') as job_lock:
                try:
                    fcntl.flock(job_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    active = True
            if active or (directory / 'result.json').exists():
                status()
            else:
                previous_path = directory / 'status.json'
                previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
                confirmed_failure = previous.get('remote_worker_failed') is True or str(
                    previous.get('error', '')).startswith('ValueError: remote conversion refused: ')
                if previous.get('state') == 'failed' and confirmed_failure:
                    conversion_generation(directory, advance=True)
                save_status(directory, dict(state='starting_conversion', started_at=time.time()))
                with (directory / 'run.log').open('a') as log:
                    process = subprocess.Popen(['bash', str(Path(__file__).with_name('cloud-disk-archive.sh')), 'run'],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                (directory / 'pid').write_text(str(process.pid))
                emit({'started_pid': process.pid, 'log': str(directory / 'run.log')})
    else:
        raise SystemExit('invalid mode')
