"""Stage one pinned local runtime/model for a short metadata-only compatibility proof."""
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import resource
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from urllib import request, error

from linux_guest_selection import file_hash, owned_file

GIB = 1024**3
ARCHIVE_BYTES = 174459907
ARCHIVE_SHA = 'b2b55434865a668decb494110d7fd970eed3ecc8837be2872f672ccbb95df974'
DECODED_BYTES = 565969764
MAIN_SHA = '69fcb23ae7b95af30b5184998751f6158df84d0071f930670ee5c22db667c2ff'
MANIFEST_SHA = '4ff64a7f502a08b7616edb8ca0a79eb1853fc363d842b7df4b46915d11a3fb09'
MANIFEST_RELATIVE = 'manifests/registry.ollama.ai/library/qwen2.5-coder/0.5b'
SOURCE_ARCHIVE = '/Users/ai/Documents/GitHub/xcanvas/.local/fleet-recovery/ollama-0.40.6/ollama-darwin.tgz'
MAX_MODEL_BYTES = 512*1024**2
RESERVE = 10*GIB
HEADROOM = 128*1024**2
# Canonical inventory derived by streaming ARCHIVE_SHA, never a mutable receipt.
RUNTIME_INVENTORY_SHA256 = '8e2986b681848675b29a05d2e626f1954c5b204b0ad042d182ee1366ca07dde5'


class BudgetRefusal(ValueError):
    pass


class ChildCleanupFailure(RuntimeError):
    """Keep the private stage intact when our child cannot be confirmed reaped."""
    pass


def free_bytes():
    row = os.statvfs('/System/Volumes/Data')
    return row.f_bavail * row.f_frsize


def require_reserve(remaining=0):
    if free_bytes() - remaining - HEADROOM < RESERVE:
        raise BudgetRefusal('private metadata staging would violate the 10 GiB disk reserve')


def archive_transfer_limit():
    # A changed remote source must not grow past the already admitted budget.
    resource.setrlimit(resource.RLIMIT_FSIZE, (ARCHIVE_BYTES, ARCHIVE_BYTES))


def metadata_resources():
    result = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=5, check=True)
    page = int(re.search(r'page size of (\d+) bytes', result.stdout).group(1))
    available = sum(int(re.search(re.escape(key)+r':\s*(\d+)', result.stdout).group(1))
                    for key in ('Pages free', 'Pages inactive', 'Pages speculative')) * page
    swapouts = int(re.search(r'Swapouts:\s*(\d+)', result.stdout).group(1))
    cpus, load = os.cpu_count() or 1, os.getloadavg()[0]
    # Metadata never starts a runner or admits inference. Its bounded streaming
    # operations and short server lifetime still need their own spare capacity.
    if available < 2*GIB or cpus < 2 or not math.isfinite(load) or not 0 <= load < cpus:
        raise BudgetRefusal('operator resources cannot safely admit the metadata-only process')
    return dict(potentially_available_bytes=available, cpu_count=cpus, load_one=load, swapouts=swapouts)


def metadata_admission():
    before = metadata_resources()
    time.sleep(1)
    after = metadata_resources()
    if after['swapouts'] != before['swapouts']:
        raise BudgetRefusal('active swap-outs defer the private metadata-only process')
    require_reserve()
    return after


def real_directory(path, *, private=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('literal absolute store directory required')
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('linked or non-directory store path refused')
    info = path.lstat()
    if info.st_uid != os.getuid() or info.st_mode & (0o077 if private else 0o022):
        raise ValueError('store directory must be owned and safely permissioned')
    return path


def checked_bytes(path, maximum=65536):
    with owned_file(path, private=False, maximum=maximum) as (stream, info):
        body = stream.read(maximum+1)
        if len(body) != info.st_size or len(body) > maximum:
            raise ValueError('bounded model metadata changed during read')
        return body


def source_model(store):
    store = real_directory(store)
    real_directory(store/'manifests/registry.ollama.ai/library/qwen2.5-coder')
    real_directory(store/'blobs')
    body = checked_bytes(store/MANIFEST_RELATIVE)
    if hashlib.sha256(body).hexdigest() != MANIFEST_SHA:
        raise ValueError('installed model manifest differs from the verified digest')
    manifest = json.loads(body)
    if (not isinstance(manifest, dict) or manifest.get('schemaVersion') != 2
            or not isinstance(manifest.get('config'), dict)
            or not isinstance(manifest.get('layers'), list) or len(manifest['layers']) > 16):
        raise ValueError('bounded local model manifest required')
    blobs, models = {}, 0
    allowed = {'application/vnd.docker.container.image.v1+json', 'application/vnd.ollama.image.model',
               'application/vnd.ollama.image.template', 'application/vnd.ollama.image.system',
               'application/vnd.ollama.image.license'}
    for row in [manifest['config']] + manifest['layers']:
        if (not isinstance(row, dict) or row.get('mediaType') not in allowed
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(row.get('digest')))
                or type(row.get('size')) is not int or not 0 < row['size'] <= MAX_MODEL_BYTES):
            raise ValueError('local bounded model blob reference required')
        models += row['mediaType'] == 'application/vnd.ollama.image.model'
        name = row['digest'].replace(':', '-')
        with owned_file(store/'blobs'/name, private=False, maximum=MAX_MODEL_BYTES) as (_, info):
            if info.st_size != row['size']:
                raise ValueError('model blob size differs from pinned manifest')
        if name in blobs and blobs[name] != row['size']:
            raise ValueError('ambiguous model blob size')
        blobs[name] = row['size']
    if models != 1 or sum(blobs.values()) > MAX_MODEL_BYTES:
        raise ValueError('exactly one installed small local model is required')
    return body, blobs


def member_name(member):
    name = member.name
    while name.startswith('./'):
        name = name[2:]
    name = name.rstrip('/')
    if not name or name == '.':
        if not member.isdir():
            raise ValueError('invalid runtime root entry')
        return None
    if (len(name) > 1024 or name.startswith('/') or '..' in PurePosixPath(name).parts
            or str(PurePosixPath(name)) != name):
        raise ValueError('runtime archive path traversal refused')
    return name


def members(archive):
    rows, total = {}, 0
    for member in archive:
        name = member_name(member)
        if name is None:
            continue
        if name in rows or len(rows) >= 10000:
            raise ValueError('duplicate or excessive runtime entries')
        if member.isfile():
            if not 0 <= member.size <= MAX_MODEL_BYTES:
                raise ValueError('runtime file exceeds bound')
            kind = 'file'; total += member.size
            if total > 2*GIB:
                raise ValueError('decoded runtime exceeds bound')
        elif member.isdir():
            kind = 'directory'
        elif member.issym() or member.islnk():
            kind = 'symlink' if member.issym() else 'hardlink'
            if not member.linkname or member.linkname.startswith('/') or len(member.linkname) > 1024:
                raise ValueError('escaping runtime archive link refused')
        else:
            raise ValueError('runtime archive special file refused')
        rows[name] = dict(kind=kind, bytes=member.size, link=member.linkname,
                          executable=bool(member.mode & 0o111))
    def target(name, row):
        result = posixpath.normpath(posixpath.join(posixpath.dirname(name), row['link'])
                                   if row['kind'] == 'symlink' else row['link'])
        if result == '..' or result.startswith('../'):
            raise ValueError('escaping runtime archive link refused')
        return result
    for name, row in rows.items():
        for parent in PurePosixPath(name).parents:
            if str(parent) != '.' and rows.get(str(parent), {}).get('kind', 'directory') != 'directory':
                raise ValueError('runtime entry through a linked parent refused')
        if row['kind'] in ('symlink', 'hardlink'):
            resolved, seen = target(name, row), {name}
            while rows.get(resolved, {}).get('kind') in ('symlink', 'hardlink'):
                if resolved in seen:
                    raise ValueError('runtime link cycle refused')
                seen.add(resolved); resolved = target(resolved, rows[resolved])
            if resolved not in rows:
                raise ValueError('runtime link target missing')
    return rows, total


def unpack(archive_path, destination):
    real_directory(destination.parent, private=True)
    digest, info = file_hash(archive_path, maximum=256*1024**2)
    if digest != ARCHIVE_SHA or info.st_size != ARCHIVE_BYTES:
        raise ValueError('runtime archive checksum mismatch')
    with tarfile.open(archive_path, 'r|gz') as archive:
        inventory, total = members(archive)
    if total != DECODED_BYTES or len(inventory) != 59:
        raise ValueError('runtime inventory differs from the reviewed archive')
    destination.mkdir(mode=0o700)
    directories = {str(parent) for name in inventory for parent in PurePosixPath(name).parents if str(parent) != '.'}
    directories.update(name for name, row in inventory.items() if row['kind'] == 'directory')
    for name in sorted(directories, key=lambda x: (x.count('/'), x)):
        (destination/name).mkdir(mode=0o700, exist_ok=True)
    with tarfile.open(archive_path, 'r|gz') as archive:
        for member in archive:
            name = member_name(member)
            if name is None or inventory[name]['kind'] != 'file':
                continue
            with archive.extractfile(member) as source, (destination/name).open('xb') as output:
                remaining = member.size
                hasher = hashlib.sha256()
                while remaining:
                    block = source.read(min(1024**2, remaining))
                    if not block:
                        raise ValueError('truncated runtime file')
                    output.write(block); hasher.update(block); remaining -= len(block)
            inventory[name]['sha256'] = hasher.hexdigest()
            (destination/name).chmod(0o700 if inventory[name]['executable'] else 0o600)
    for name, row in inventory.items():
        if row['kind'] == 'hardlink':
            target = destination/posixpath.normpath(row['link'])
            if not target.is_file() or target.is_symlink():
                raise ValueError('hardlink must target an extracted regular file')
            os.link(target, destination/name)
        elif row['kind'] == 'symlink':
            (destination/name).symlink_to(row['link'])
    if file_hash(destination/'ollama', maximum=128*1024**2)[0] != MAIN_SHA:
        raise ValueError('signed runtime executable checksum mismatch')
    verified = 0
    magics = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'}
    for name, row in inventory.items():
        if row['kind'] != 'file':
            continue
        path = destination/name
        with path.open('rb') as stream:
            magic = stream.read(4)
        if magic in magics:
            subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(path)],
                           capture_output=True, timeout=15, check=True)
            verified += 1
    release_inventory = dict(schema='virtualos-pinned-ollama-runtime-inventory/v1',
                             archive_sha256=ARCHIVE_SHA, decoded_bytes=total, entries=inventory)
    checked = verify_runtime(destination, release_inventory)
    with private_output(destination.parent/'runtime-inventory.json') as output:
        output.write(json.dumps(release_inventory, sort_keys=True, separators=(',', ':')).encode()+b'\n')
    return dict(archive_sha256=ARCHIVE_SHA, decoded_bytes=total, entries=len(inventory),
                main_sha256=MAIN_SHA, strict_codesign_verified_files=verified, **checked)


def runtime_inventory_digest(inventory):
    return hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify_runtime(destination, inventory):
    """Verify all immutable runtime contents against the release-derived pin."""
    if (not isinstance(inventory, dict) or RUNTIME_INVENTORY_SHA256 is None
            or runtime_inventory_digest(inventory) != RUNTIME_INVENTORY_SHA256):
        raise ValueError('runtime inventory differs from the authenticated release pin')
    destination = real_directory(destination, private=True)
    entries = inventory['entries']
    expected = set(entries)
    expected.update(str(parent) for name in entries for parent in PurePosixPath(name).parents
                    if str(parent) != '.')
    actual = set()
    def walk_failure(failure):
        raise ValueError('private runtime directory could not be inspected') from failure
    for current, directories, files in os.walk(destination, followlinks=False, onerror=walk_failure):
        actual.update(str((Path(current)/name).relative_to(destination)) for name in directories+files)
    if actual != expected:
        raise ValueError('private runtime contains missing or unexpected entries')
    checked_files, checked_bytes = 0, 0
    for name in sorted(expected):
        path = destination/name
        row = entries.get(name, {'kind': 'directory'})
        info = path.lstat()
        if info.st_uid != os.getuid():
            raise ValueError('private runtime entry ownership changed')
        if row['kind'] == 'directory':
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('private runtime directory changed')
        elif row['kind'] == 'symlink':
            if not stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or os.readlink(path) != row['link']:
                raise ValueError('private runtime link changed')
        elif row['kind'] == 'file':
            if stat.S_IMODE(info.st_mode) != (0o700 if row['executable'] else 0o600):
                raise ValueError('private runtime file permissions changed')
            digest, info = file_hash(path, maximum=MAX_MODEL_BYTES)
            if digest != row['sha256'] or info.st_size != row['bytes']:
                raise ValueError('private runtime file content changed')
            checked_files += 1
            checked_bytes += info.st_size
        else:
            raise ValueError('unsupported private runtime inventory entry')
    return dict(runtime_inventory_sha256=RUNTIME_INVENTORY_SHA256,
                runtime_files_verified=checked_files, runtime_bytes_verified=checked_bytes,
                runtime_entries_verified=len(expected))


def copy_model(source, destination, body, blobs):
    destination.mkdir(mode=0o700)
    (destination/'blobs').mkdir(mode=0o700)
    for name, size in blobs.items():
        original, target = source/'blobs'/name, destination/'blobs'/name
        if file_hash(original, private=False, maximum=MAX_MODEL_BYTES)[0] != name.removeprefix('sha256-'):
            raise ValueError('source model blob checksum mismatch')
        subprocess.run(['/bin/cp', '-c', str(original), str(target)], capture_output=True, timeout=30, check=True)
        target.chmod(0o600)
        pinned, info = file_hash(target, maximum=MAX_MODEL_BYTES)
        original_info = original.lstat()
        if (pinned != name.removeprefix('sha256-') or info.st_size != size
                or (info.st_dev, info.st_ino) == (original_info.st_dev, original_info.st_ino)):
            raise ValueError('private COW model copy differs from source pin')
    manifest = destination/MANIFEST_RELATIVE
    manifest.parent.mkdir(mode=0o700, parents=True)
    for path in (manifest, destination/'blobs'/('sha256-'+MANIFEST_SHA)):
        with path.open('xb') as output:
            output.write(body)
        path.chmod(0o600)
        if file_hash(path)[0] != MANIFEST_SHA:
            raise ValueError('private model manifest pin mismatch')


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HTTP = request.build_opener(request.ProxyHandler({}), NoRedirect)


def metadata(port, path, body=None):
    if path not in ('/api/version', '/api/tags', '/api/ps', '/api/show'):
        raise ValueError('only metadata routes are permitted')
    if path == '/api/show' and body != {'model': 'sha256-'+MANIFEST_SHA, 'verbose': False}:
        raise ValueError('only the pinned full-digest metadata request is permitted')
    if path != '/api/show' and body is not None:
        raise ValueError('metadata route must remain read-only')
    req = request.Request('http://127.0.0.1:'+str(port)+path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json'})
    try:
        with HTTP.open(req, timeout=3) as response:
            data = response.read(65537)
        if len(data) > 65536:
            raise ValueError('private metadata response exceeds bound')
        return json.loads(data)
    except error.HTTPError as failure:
        failure.close()
        raise


def owned_group_live(group):
    result = subprocess.run(['ps', '-axo', 'pgid=,stat='], capture_output=True,
                            text=True, timeout=5, check=True)
    if len(result.stdout) > 1024**2:
        raise ValueError('owned process-group inspection exceeds bound')
    return any(len(fields) == 2 and fields[0] == str(group) and not fields[1].startswith('Z')
               for fields in (line.split() for line in result.stdout.splitlines()))


def signal_owned_group(child, which):
    # The still-unreaped leader reserves this group ID until all signals finish.
    if child.returncode is not None:
        raise ValueError('private metadata leader was reaped before group cleanup')
    try:
        os.killpg(child.pid, which)
    except ProcessLookupError:
        pass
    except PermissionError:
        if owned_group_live(child.pid):
            raise


def stop_owned(child):
    try:
        signal_owned_group(child, signal.SIGTERM)
        time.sleep(0.3)
        signal_owned_group(child, signal.SIGKILL)
        child.wait(timeout=5)
    except Exception as failure:
        cleanup = ChildCleanupFailure('private metadata child could not be confirmed reaped')
        cleanup.child_pid = child.pid
        raise cleanup from failure


def private_output(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('private metadata log descriptor is unsafe')
        os.ftruncate(fd, 0)
        return os.fdopen(fd, 'wb')
    except Exception:
        os.close(fd)
        raise


def private_server(stage):
    stage = real_directory(stage, private=True)
    metadata_admission()
    profile = ('(version 1) (allow default) (deny network-outbound) (deny file-write*) '
               '(allow file-write* (subpath '+json.dumps(str(stage))+'))')
    sandbox = '/usr/bin/sandbox-exec'
    outside = stage.with_name(stage.name+'.forbidden-write')
    if outside.exists() or outside.is_symlink():
        raise ValueError('sandbox canary path collision')
    with private_output(stage/'sandbox-check.log') as log:
        denied = subprocess.run([sandbox, '-p', profile, '/usr/bin/touch', str(outside)],
                                stdout=log, stderr=log, timeout=10)
    if outside.exists():
        outside.unlink()
        raise ValueError('private server sandbox did not deny outside writes')
    if denied.returncode == 0:
        raise ValueError('private server sandbox did not deny outside writes')
    inside = stage/('write-canary-'+uuid.uuid4().hex)
    subprocess.run([sandbox, '-p', profile, '/usr/bin/touch', str(inside)],
                   capture_output=True, timeout=10, check=True)
    inside.unlink()
    for name in ('tmp', 'cache'):
        (stage/name).mkdir(mode=0o700, exist_ok=True)
        real_directory(stage/name, private=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL')}
    env.update(OLLAMA_HOST='127.0.0.1:'+str(port), OLLAMA_MODELS=str(stage/'models'),
               OLLAMA_NO_CLOUD='1', OLLAMA_NOPRUNE='1', OLLAMA_MAX_LOADED_MODELS='1',
               OLLAMA_NUM_PARALLEL='1', OLLAMA_KEEP_ALIVE='0', OLLAMA_CONTEXT_LENGTH='2048',
               OLLAMA_MAX_QUEUE='1', TMPDIR=str(stage/'tmp'), XDG_CACHE_HOME=str(stage/'cache'))
    with private_output(stage/'server.log') as log:
        child = subprocess.Popen([sandbox, '-p', profile, str(stage/'runtime/ollama'), 'serve'],
                                 env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, umask=0o077)
    try:
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            try:
                version = metadata(port, '/api/version')
                if version.get('version') != '0.40.6':
                    raise ValueError('private server version differs from pinned runtime')
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise ValueError('private metadata server readiness timed out')
        listener = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(child.pid), '-iTCP:'+str(port),
                                   '-sTCP:LISTEN', '-nP'], capture_output=True, text=True, timeout=5)
        if listener.returncode != 0 or '127.0.0.1:'+str(port) not in listener.stdout or '*:'+str(port) in listener.stdout:
            raise ValueError('private server listener must belong only to our loopback child')
        if metadata(port, '/api/ps').get('models') != []:
            raise ValueError('private metadata endpoint unexpectedly loaded a model')
        shown = metadata(port, '/api/show', {'model': 'sha256-'+MANIFEST_SHA, 'verbose': False})
        if metadata(port, '/api/ps').get('models') != []:
            raise ValueError('metadata proof must not load a model')
        require_reserve()
        return dict(private_server_version='0.40.6', private_server_pid=child.pid, private_port=port,
                    full_manifest_reference='sha256-'+MANIFEST_SHA, full_digest_reference_supported=True,
                    capabilities=shown.get('capabilities', []), models_loaded=0,
                    global_writes_denied_by_sandbox=True, credential_environment_scrubbed=True)
    finally:
        stop_owned(child)


def global_snapshot():
    return dict(version=metadata(11434, '/api/version'), loaded=metadata(11434, '/api/ps'))


def stage_and_show():
    os.umask(0o077)
    source = Path.home()/'.ollama/models'
    body, blobs = source_model(source)
    needed = ARCHIVE_BYTES+DECODED_BYTES+sum(blobs.values())
    require_reserve(needed)
    resources = metadata_admission()
    before = global_snapshot()
    stage = Path(tempfile.mkdtemp(prefix='vos-private-ollama-', dir='/private/tmp'))
    try:
        config = Path.home()/'Documents/GitHub/realestate-machine/.local/mini-remote-20260915/ssh_config'
        archive = stage/'ollama-darwin.tgz'
        subprocess.run(['scp', '-F', str(config), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                        '-o', 'ConnectTimeout=10', '-o', 'ProxyCommand=none', '-o', 'HostName=192.168.1.123',
                        'mac-mini-cloudflare:'+SOURCE_ARCHIVE, str(archive)], capture_output=True,
                       timeout=120, check=True, preexec_fn=archive_transfer_limit)
        archive.chmod(0o600)
        require_reserve(DECODED_BYTES+sum(blobs.values()))
        checked = unpack(archive, stage/'runtime')
        require_reserve(sum(blobs.values()))
        copy_model(source, stage/'models', body, blobs)
        require_reserve(); metadata_resources()
        proof = private_server(stage)
        after = global_snapshot()
        if before != after:
            raise ValueError('global Ollama metadata changed during private proof')
        result = dict(schema='virtualos-private-ollama-metadata-proof/v1', state='metadata_verified',
                      observed_at=time.time(), stage_directory=str(stage), runtime=checked, proof=proof,
                      manifest_sha256=MANIFEST_SHA, model_blob_bytes=sum(blobs.values()),
                      model_blobs_verified=len(blobs), private_process_stopped_and_reaped=True,
                      global_daemon_metadata_unchanged=True, global_model_store_writes=False,
                      inference_requested=False, model_download_requested=False, guest_input_applied=False,
                      post_stage_free_bytes=free_bytes(), minimum_free_disk_bytes=RESERVE,
                      initial_metadata_resources=resources)
        (stage/'metadata-proof.json').write_text(json.dumps(result, sort_keys=True)+'\n')
        return result
    except ChildCleanupFailure as failure:
        # An unreaped child may still use this stage. Retain it for inspection.
        failure.stage_directory = str(stage)
        raise
    except Exception:
        # This directory is newly created by this invocation, never supplied by
        # a caller. The private server, if started, was reaped in its finally.
        real_directory(stage, private=True)
        shutil.rmtree(stage)
        raise


if __name__ == '__main__':
    try:
        if sys.argv[1:] != ['--stage-and-show']:
            raise ValueError('explicit --stage-and-show required; inference is never supported')
        print(json.dumps(stage_and_show(), sort_keys=True))
    except BudgetRefusal as failure:
        print(json.dumps({'state': 'waiting_for_resources', 'reason': str(failure), 'inference_requested': False}))
        raise SystemExit(75)
    except Exception as failure:
        result = {'state': 'requires_attention', 'error': type(failure).__name__, 'inference_requested': False}
        if isinstance(failure, ChildCleanupFailure):
            result.update(stage_directory=getattr(failure, 'stage_directory', None),
                          own_child_pid=getattr(failure, 'child_pid', None), private_process_stopped_and_reaped=False)
        print(json.dumps(result))
        raise SystemExit(1)
