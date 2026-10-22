"""Pinned base readback and isolated macOS NBD recovery proof on the Mini."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import threading
import time
from urllib import request, error
import math
import errno

from cloud_disk import CHUNK, MAX_MANIFEST_BYTES, CloudDisk, digest, serve_nbd_connection


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


LOCAL_HTTP = request.build_opener(request.ProxyHandler({}), NoRedirect)


class ResourcePressure(RuntimeError):
    """A retryable resource refusal after our own disposable child is stopped."""


def pressure_interrupted_network(failure):
    reason = failure.reason if isinstance(failure, error.URLError) else failure
    if getattr(reason, 'errno', None) in (errno.EIO, errno.EACCES, errno.EPERM):
        return False
    return ((isinstance(failure, error.URLError) and not isinstance(failure, error.HTTPError))
            or getattr(failure, 'errno', None) in (errno.ECONNREFUSED, errno.ECONNRESET, errno.EPIPE))


def resource_report():
    try:
        result = subprocess.run(['bash', '/Users/ai/vos-fleet/tools/computer-resources.sh', '--report'],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        row = json.loads(result.stdout) if result.returncode == 0 else {}
        keys = ('disk_free_kib', 'memory_free_percent', 'swap_used_mib', 'load_one', 'cpu_count')
        if any(type(row.get(key)) not in (int, float) or not math.isfinite(row[key]) for key in keys):
            return {}
        if type(row['cpu_count']) is not int or row['cpu_count'] < 1 or not 0 <= row['memory_free_percent'] <= 100:
            return {}
        return {key: row[key] for key in keys}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def resources_allowed(row, running=False):
    # 4 GiB RAM / 2 CPU proof. Cache writes are optional and separately preserve
    # 10 GiB host reserve + 2 GiB write headroom; overlays also enforce 10 GiB.
    # Admission never promises that the configured cache/overlay will fill.
    try:
        return (row['cpu_count'] >= 2 and row['disk_free_kib'] >= (12 if running else 14) * 1024**2
                and row['memory_free_percent'] >= (30 if running else 50)
                and 0 <= row['swap_used_mib'] < (12 if running else 8) * 1024
                and 0 <= row['load_one'] < row['cpu_count'] * (2 if running else 1))
    except (KeyError, TypeError):
        return False


def pressure_watch(supervisor, stop, aborted):
    while not stop.wait(5):
        if not resources_allowed(resource_report(), running=True):
            aborted.set()
            # This Popen object refers only to our newly created disposable
            # supervisor. Never signal a process discovered by name or port.
            if supervisor.poll() is None:
                supervisor.terminate()
            return


def running_fleet_guests(existing):
    states = {'running', 'starting', 'pending', 'idle', 'stopping', 'failed'}
    if (not isinstance(existing, dict) or existing.get('ok') is not True
            or not isinstance(existing.get('instances'), list)):
        raise ValueError('invalid fleet capacity response')
    for row in existing['instances']:
        if not isinstance(row, dict) or row.get('state') not in states or not isinstance(row.get('name'), str):
            raise ValueError('invalid fleet instance state')
    return sum(row['state'] in ('running', 'starting', 'pending', 'stopping') for row in existing['instances'])


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('recovery directory must be real and owned by this user')
    path.chmod(0o700)
    for child in path.iterdir():
        info = child.lstat()
        if (not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or
                info.st_uid != os.getuid() or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise ValueError('unsafe recovery state entry')
    return path


def overlay_fingerprint(path):
    hasher = hashlib.sha256()
    blocks = 0
    with closing(sqlite3.connect(str(path))) as db:
        for index, data in db.execute('SELECT idx,data FROM blocks ORDER BY idx'):
            hasher.update(index.to_bytes(8, 'big'))
            hasher.update(data)
            blocks += 1
    return dict(blocks=blocks, sha256=hasher.hexdigest())


def recover(store, config, manifest_sha256, pins, *, progress,
            recovery_root='/Users/ai/vos-shared-fleet/archive-recovery'):
    readback_verified = False

    def observed(row):
        nonlocal readback_verified
        if row.get('state') in ('base_readback_verified', 'base_readback_reused'):
            readback_verified = row.get('full_r2_readback_verified') is True
        progress(row)

    try:
        return _recover(store, config, manifest_sha256, pins, progress=observed,
                        recovery_root=recovery_root)
    except OSError as failure:
        if failure.errno not in (errno.ENOSPC, errno.EDQUOT):
            raise
        result = dict(state='waiting_for_resources', resource_reason='host_storage_pressure',
                      storage_errno=failure.errno, manifest_sha256=manifest_sha256,
                      disk_sha256=pins['disk_sha256'], logical_bytes=pins['disk_bytes'],
                      full_r2_readback_verified=readback_verified, guest_boot_verified=False,
                      production_restarted=False)
        progress(result)
        return result


def _recover(store, config, manifest_sha256, pins, *, progress, recovery_root):
    root = private_directory(Path(recovery_root))
    private = private_directory(root / manifest_sha256)
    free = os.statvfs(private)
    if free.f_bavail * free.f_frsize < 10 * 1024**3 + 64 * 1024**2:
        raise OSError(errno.ENOSPC, 'recovery metadata must preserve host reserve')
    base = private_directory(private / 'base')
    manifest = store.get('manifest.json', MAX_MANIFEST_BYTES)
    if digest(manifest) != manifest_sha256:
        raise ValueError('recovery manifest does not match conversion pin')
    (base / 'manifest.json').write_bytes(manifest)
    factory = lambda overlay, cache=None: CloudDisk(base, overlay,
        fetch=lambda key: store.get('objects/' + key + '.z', CHUNK + 65536),
        cache_dir=cache, cache_limit_bytes=config['local_cache_limit_bytes'],
        overlay_limit_bytes=config['local_overlay_limit_bytes'],
        min_host_free_bytes=10 * 1024**3, cache_write_reserve_bytes=2 * 1024**3)
    audit_path = private / 'base-readback.json'
    expected = dict(manifest_sha256=manifest_sha256, disk_sha256=pins['disk_sha256'],
                    logical_bytes=pins['disk_bytes'], full_r2_readback_verified=True,
                    source={key: config[key] for key in ('account_id', 'bucket', 'object_prefix')})
    try:
        prior_audit = json.loads(audit_path.read_text()) if audit_path.exists() else None
    except (ValueError, UnicodeError):
        prior_audit = None  # A torn receipt never suppresses a fresh full hash.
    if prior_audit == expected:
        progress(dict(state='base_readback_reused', **expected))
    else:
        progress(dict(state='base_readback_started', logical_bytes=pins['disk_bytes']))
        disk = factory(private / 'readback-overlay.sqlite')
        hasher = hashlib.sha256()
        try:
            if disk.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]:
                raise ValueError('base readback overlay must be empty')
            if disk.size != pins['disk_bytes']:
                raise ValueError('readback disk size differs from archive pin')
            for offset in range(0, disk.size, CHUNK):
                hasher.update(disk.read(offset, min(CHUNK, disk.size - offset)))
                if offset // CHUNK % 32 == 0:
                    progress(dict(state='base_readback', verified_bytes=min(offset + CHUNK, disk.size),
                                  logical_bytes=disk.size))
            if hasher.hexdigest() != pins['disk_sha256']:
                raise ValueError('full R2 raw disk SHA-256 differs from archive pin')
        finally:
            disk.close()
        temporary_audit = audit_path.with_suffix('.json.next')
        with temporary_audit.open('w') as stream:
            stream.write(json.dumps(expected, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary_audit.replace(audit_path)
        progress(dict(state='base_readback_verified', **expected))
    # Production and an existing fleet macOS VM consume the host's two macOS
    # slots. Leave them running; a later invocation retries only the boot proof.
    processes = subprocess.check_output(['ps', '-axo', 'comm='], text=True, timeout=10)
    production = sum(line.strip() == '/Applications/virtualOS.app/Contents/MacOS/virtualOS'
                     for line in processes.splitlines())
    try:
        existing = http_json(9225, '/fleet/status')
        running = running_fleet_guests(existing)
    except (OSError, ValueError, KeyError):
        result = dict(expected, guest_boot_verified=False, state='waiting_for_capacity',
                      capacity_probe='existing fleet supervisor unavailable', production_restarted=False)
        progress(result)
        return result
    if production + running >= 2:
        result = dict(expected, guest_boot_verified=False, state='waiting_for_capacity',
                      running_macos_guests=production + running, production_restarted=False)
        progress(result)
        return result
    resources = resource_report()
    cache_budget = min(config['local_cache_limit_bytes'],
                       max(0, resources.get('disk_free_kib', 0) * 1024 - 12 * 1024**3))
    budget = dict(cpus=2, memory_gib=4, admission_free_memory_percent=50,
                  admission_free_disk_gib=14, minimum_host_free_gib=10,
                  cache_budget_bytes=cache_budget,
                  cache_limited_by_reserve=cache_budget < config['local_cache_limit_bytes'])
    if not resources_allowed(resources):
        result = dict(expected, guest_boot_verified=False, state='waiting_for_resources',
                      running_macos_guests=production + running, production_restarted=False,
                      resource_gate=resources, recovery_budget=budget)
        progress(result)
        return result
    progress(dict(state='guest_resource_admitted', resource_gate=resources, recovery_budget=budget))
    try:
        return boot_proof(private, factory, expected, progress)
    except ResourcePressure:
        result = dict(expected, guest_boot_verified=False, state='waiting_for_resources',
                      production_restarted=False, resource_gate=resource_report(),
                      recovery_budget=budget)
        progress(result)
        return result


def http_json(port, path, method='GET', timeout=15):
    try:
        with LOCAL_HTTP.open(request.Request('http://127.0.0.1:' + str(port) + path,
                                            method=method), timeout=timeout) as response:
            body = response.read(1024**2 + 1)
            if len(body) > 1024**2:
                raise ValueError('local supervisor response exceeds bound')
            return json.loads(body)
    except error.HTTPError as failure:
        failure.close()
        raise


def require_owned_listener(child, port):
    """A free-port probe alone cannot bind a later response to our child."""
    if (type(child.pid) is not int or child.pid <= 0 or type(port) is not int
            or not 1 <= port <= 65535 or child.poll() is not None):
        raise ConnectionError('disposable supervisor is not a live owned child')
    listener = subprocess.run(['/usr/sbin/lsof','-a','-p',str(child.pid),
        '-iTCP:'+str(port),'-sTCP:LISTEN','-nP','-Fpn'],
        capture_output=True,text=True,timeout=5)
    if len(listener.stdout)>65536:
        raise ValueError('disposable listener inspection exceeds bound')
    fields=listener.stdout.splitlines()
    processes=[field[1:] for field in fields if field.startswith('p')]
    addresses=[field[1:] for field in fields if field.startswith('n')]
    if (listener.returncode or processes!=[str(child.pid)] or not addresses
            or set(addresses)!={'127.0.0.1:'+str(port)}):
        raise ConnectionError('owned disposable loopback listener is unavailable')


def owned_http_json(child, port, path, method='GET'):
    require_owned_listener(child,port)
    return http_json(port,path,method)


def wait_frame(port, name, check_health, seconds=900):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check_health()
        try:
            with LOCAL_HTTP.open('http://127.0.0.1:' + str(port) + '/fleet/frame/' + name,
                                 timeout=min(10, max(0.01, deadline - time.monotonic()))) as response:
                image = response.read(8 * 1024**2 + 1)
            if 30000 < len(image) <= 8 * 1024**2:
                return image
        except error.HTTPError as failure:
            failure.close()
        except OSError:
            pass
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError('guest desktop frame not ready within 15 minutes')


def boot_proof(private, factory, expected, progress):
    name, port, nbd_port = 'r2-archive-proof', 9257, 40995
    bundle = private / (name + '.bundle')
    private_directory(bundle)
    platform = Path('/Users/ai/vos-shared-fleet/vm-base.bundle')
    for filename in ('HardwareModel', 'MachineIdentifier', 'AuxiliaryStorage'):
        source, target = platform / filename, bundle / filename
        if source.is_symlink() or not source.is_file() or target.is_symlink():
            raise ValueError('platform metadata must be regular files')
        if not target.exists():
            shutil.copyfile(source, target)
    if (bundle / 'Disk.img').exists():
        raise ValueError('recovery proof must remain diskless')
    params = dict(cpuCount=2, memorySizeInGB=4, screenWidth=1500, screenHeight=900,
                  nbdURL='nbd://127.0.0.1:' + str(nbd_port) + '/virtualos-test')
    (bundle / 'Parameters.txt').write_text(json.dumps(params))
    cache = private_directory(private / 'cache')
    overlay = private / 'guest-overlay.sqlite'
    binary = Path('/Users/ai/vos-shared-fleet/tools/fleet/fleet-supervisor')
    if not os.access(binary, os.X_OK):
        raise ValueError('existing signed fleet supervisor is required')
    # Bind checks refuse a collision rather than acting on another supervisor.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
    stats, frames = [], []
    with (private / 'supervisor.log').open('a') as log:
        supervisor = subprocess.Popen([str(binary), '--root', str(private), '--names', name,
            '--http-port', str(port), '--base-port', '9190'], stdout=log, stderr=log,
            env={key: value for key, value in os.environ.items() if not
                (key.startswith(('AWS_', 'CF_', 'CLOUDFLARE_')) or
                 any(word in key.upper() for word in ('SECRET', 'TOKEN', 'PASSWORD', 'API_KEY')))})
    listener = None
    stop = None
    server = None
    monitor_stop, pressure_abort = threading.Event(), threading.Event()
    monitor = threading.Thread(target=pressure_watch, args=(supervisor, monitor_stop, pressure_abort), daemon=True)
    monitor.start()
    try:
        for attempt in range(60):
            if pressure_abort.is_set():
                raise ResourcePressure('disposable recovery stopped by host resource guard')
            if supervisor.poll() is not None:
                raise RuntimeError('isolated supervisor exited before readiness')
            try:
                owned_http_json(supervisor, port, '/fleet/status')
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError('isolated supervisor readiness timed out')
        previous = None
        for boot in (1, 2):
            if pressure_abort.is_set() or not resources_allowed(resource_report()):
                raise ResourcePressure('disposable recovery stopped by host resource guard')
            disk = factory(overlay, cache)
            disk.close()
            retained = overlay_fingerprint(overlay)
            if previous is not None and retained != previous:
                raise ValueError('guest writes changed across disk-service restart')
            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', nbd_port))
            listener.listen(1)
            listener.settimeout(1)
            stop = threading.Event()
            errors = []

            def serve():
                try:
                    while not stop.is_set():
                        try:
                            connection, _ = listener.accept()
                        except socket.timeout:
                            continue
                        session = {}
                        serve_nbd_connection(connection, None, overlay, stats=session,
                            disk_factory=lambda: factory(overlay, cache), idle_timeout=None)
                        stats.append(session)
                except Exception as exc:
                    if not stop.is_set():
                        errors.append(type(exc).__name__ + ': ' + str(exc))

            server = threading.Thread(target=serve, daemon=True)
            server.start()
            progress(dict(state='guest_boot_started', boot=boot, preserved_overlay=retained))
            owned_http_json(supervisor, port, '/fleet/start/' + name, 'POST')
            def healthy():
                if pressure_abort.is_set():
                    raise ResourcePressure('disposable recovery stopped by host resource guard')
                if supervisor.poll() is not None:
                    raise RuntimeError('disposable recovery stopped before frame readiness')
                require_owned_listener(supervisor,port)
                if errors:
                    raise RuntimeError('NBD service failed: ' + errors[0])
            image = wait_frame(port, name, healthy)
            frame = private / ('boot-' + str(boot) + '.jpg')
            frame.write_bytes(image)
            frames.append(dict(boot=boot, path=str(frame), bytes=len(image), sha256=digest(image)))
            owned_http_json(supervisor, port, '/fleet/stop/' + name, 'POST')
            for attempt in range(60):
                state = owned_http_json(supervisor, port, '/fleet/status')['instances'][0]['state']
                if state == 'idle':
                    break
                time.sleep(1)
            else:
                raise RuntimeError('disposable guest did not stop')
            stop.set()
            server.join(timeout=30)
            listener.close()
            listener = None
            if server.is_alive() or errors:
                raise RuntimeError('NBD disk-service shutdown did not complete cleanly')
            previous = overlay_fingerprint(overlay)
            if not previous['blocks']:
                raise ValueError('guest did not persist any overlay writes')
        if (len(stats) < 2 or not all(row.get('negotiated') and row.get('reads', 0) > 0 for row in stats)
                or any(row.get('metrics', {}).get(kind, {}).get('errors', 0)
                       for row in stats for kind in ('read', 'write'))):
            raise ValueError('two negotiated NBD guest boots are required')
        return dict(expected, state='frames_ready_for_visual_review', guest_boot_verified=False,
            guest_boot_frames=frames, disk_service_restart_verified=True, overlay=previous,
            production_restarted=False, local_disk_extracted=False, nbd_sessions=stats)
    except OSError as failure:
        if pressure_abort.is_set() and pressure_interrupted_network(failure):
            raise ResourcePressure('disposable recovery stopped by host resource guard') from None
        raise
    finally:
        cleanup_owned_proof(supervisor, port, name, monitor_stop, monitor, stop, listener, server)


def cleanup_owned_proof(supervisor, port, name, monitor_stop, monitor, stop, listener, server):
    # Neither malformed local JSON nor a listener failure may skip reaping our
    # own Popen child. A failed cleanup is terminal, rather than a safe retry.
    try:
        monitor_stop.set()
        monitor.join(timeout=20)
    finally:
        try:
            try:
                owned_http_json(supervisor, port, '/fleet/stop/' + name, 'POST')
            except (OSError, ValueError, TypeError):
                pass
        finally:
            try:
                if stop is not None:
                    stop.set()
                if listener is not None:
                    listener.close()
            finally:
                if supervisor.poll() is None:
                    supervisor.terminate()
                try:
                    supervisor.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    supervisor.kill()
                    supervisor.wait(timeout=10)
    if server is not None:
        server.join(timeout=30)
        if server.is_alive():
            raise RuntimeError('disposable NBD server cleanup did not finish')
