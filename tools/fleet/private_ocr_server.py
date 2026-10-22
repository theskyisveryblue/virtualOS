"""Short-lived private OCR backend; never replaces or stops a global daemon."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ollama_private_metadata as pinned
from linux_guest_selection import read_json, file_hash
from ocr_budget import resources, check_resources, GIB


class PrivateOCRError(RuntimeError):
    pass


def private_output(path):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                or info.st_mode&0o077 or info.st_nlink!=1):
            raise PrivateOCRError('private backend log descriptor is unsafe')
        os.ftruncate(fd,0)
        return os.fdopen(fd,'wb')
    except Exception:
        os.close(fd)
        raise


def verify_stage(stage):
    stage = pinned.real_directory(stage, private=True)
    receipt = read_json(stage/'metadata-proof.json', private=True)
    proof, runtime = receipt.get('proof', {}), receipt.get('runtime', {})
    if (receipt.get('schema') != 'virtualos-private-ollama-metadata-proof/v1'
            or receipt.get('state') != 'metadata_verified'
            or receipt.get('private_process_stopped_and_reaped') is not True
            or receipt.get('inference_requested') is not False
            or receipt.get('manifest_sha256') != pinned.MANIFEST_SHA
            or runtime.get('archive_sha256') != pinned.ARCHIVE_SHA
            or runtime.get('main_sha256') != pinned.MAIN_SHA
            or proof.get('private_server_version') != '0.40.6'
            or proof.get('full_digest_reference_supported') is not True
            or proof.get('full_manifest_reference') != 'sha256-'+pinned.MANIFEST_SHA):
        raise PrivateOCRError('a completed pinned private metadata proof is required')
    pinned.verify_runtime(stage/'runtime', read_json(stage/'runtime-inventory.json',private=True))
    executable = stage/'runtime/ollama'
    if file_hash(executable, maximum=128*1024**2)[0] != pinned.MAIN_SHA:
        raise PrivateOCRError('private executable changed after metadata verification')
    subprocess.run(['/usr/bin/codesign','--verify','--strict',str(executable)],
                   capture_output=True, timeout=15, check=True)
    _, blobs = pinned.source_model(stage/'models')
    for name in blobs:
        if file_hash(stage/'models/blobs'/name, maximum=pinned.MAX_MODEL_BYTES)[0] != name[7:]:
            raise PrivateOCRError('private model blob changed after metadata verification')
    if file_hash(stage/'models/blobs'/('sha256-'+pinned.MANIFEST_SHA))[0] != pinned.MANIFEST_SHA:
        raise PrivateOCRError('private full-reference manifest changed')
    return stage, sum(blobs.values())


def sandbox_profile(stage):
    return ('(version 1) (allow default) (deny file-write*) '
            '(allow file-write* (subpath '+json.dumps(str(stage))+')) '
            '(deny network-outbound) '
            '(allow network-outbound (remote ip "localhost:*"))')


def check_sandbox(stage, profile, environment):
    command = ['/usr/bin/sandbox-exec','-p',profile]
    outside = stage.parent/('.ocr-write-denied-'+uuid.uuid4().hex)
    if outside.exists() or outside.is_symlink():
        raise PrivateOCRError('sandbox canary path collision')
    with private_output(stage/'inference-sandbox-check.log') as log:
        denied = subprocess.run(command+['/usr/bin/touch',str(outside)],
                                env=environment, stdout=log, stderr=log, timeout=10)
    if outside.exists():
        outside.unlink()
        raise PrivateOCRError('private backend sandbox permitted an outside write')
    if denied.returncode == 0:
        raise PrivateOCRError('private backend write sandbox could not be verified')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0)); listener.listen(1)
        code = ('import socket; s=socket.create_connection(("127.0.0.1",'
                +str(listener.getsockname()[1])+'),timeout=2); s.close()')
        subprocess.run(command+[sys.executable,'-c',code], env=environment,
                       capture_output=True, timeout=5, check=True)
    code = ('import socket,errno,sys\ns=socket.socket();s.settimeout(2)\n'
            'try:\n s.connect(("1.1.1.1",443))\n'
            'except OSError as e:\n sys.exit(0 if e.errno in (errno.EPERM,errno.EACCES) else 2)\n'
            'sys.exit(1)')
    denied = subprocess.run(command+[sys.executable,'-c',code], env=environment,
                            capture_output=True, timeout=5)
    if denied.returncode != 0:
        raise PrivateOCRError('external network denial could not be verified')


def pressure_reason(row, initial_swapouts):
    for key in ('available_bytes','disk_free_bytes','cpus','swapouts'):
        if type(row.get(key)) is not int or row[key] < 0:
            return 'resource_statistics_unavailable'
    for key in ('load','swap_used_bytes'):
        if type(row.get(key)) not in (int,float) or not math.isfinite(row[key]) or row[key] < 0:
            return 'resource_statistics_unavailable'
    if (row['available_bytes'] < 4*GIB or row['disk_free_bytes'] < 10*GIB
            or row['cpus'] < 2 or row['load'] >= 2*row['cpus']
            or row['swap_used_bytes'] >= 8*GIB or row['swapouts'] != initial_swapouts):
        return 'host_pressure'
    return None


def group_has_live_members(group):
    rows=subprocess.check_output(['ps','-axo','pgid=,stat='],text=True,timeout=5)
    if len(rows)>1048576:
        raise PrivateOCRError('owned group inspection exceeds bound')
    for line in rows.splitlines():
        values=line.split()
        if len(values)==2 and values[0]==str(group) and not values[1].startswith('Z'):
            return True
    return False


def signal_owned_group(child, which):
    if child.returncode is not None:
        return
    try:
        os.killpg(child.pid,which)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin may return EPERM for an already exited group containing only
        # zombies. Verify that state rather than misreporting an active runner.
        if group_has_live_members(child.pid):
            raise


def terminate_owned_group(child):
    # A live, unreaped Popen reserves its PID. Its new session contains only
    # this backend and its descendants. Never signal a name or borrowed PID.
    if child.returncode is None:
        signal_owned_group(child,signal.SIGTERM)
        # Do not reap the leader before clearing its descendants: its reserved
        # PID prevents a completed group's ID being reused by another project.
        time.sleep(.3)
        signal_owned_group(child,signal.SIGKILL)


def stop_owned_group(child):
    terminate_owned_group(child)
    child.wait(timeout=5)


def interrupt_owned_group(child, stop, signal_lock):
    # Cleanup sets stop before taking this lock. A probe finishing late can
    # never signal a group after the owner has reaped its reserved leader.
    with signal_lock:
        if not stop.is_set():
            terminate_owned_group(child)


def cleanup_owned_session(child, stop, signal_lock, monitor):
    stop.set()
    with signal_lock:
        stop_owned_group(child)
    if monitor is not None:
        monitor.join(timeout=15)
        if monitor.is_alive():
            raise PrivateOCRError("private pressure probe did not stop; retain stage")


def require_global_idle():
    if pinned.metadata(11434,'/api/ps').get('models') != []:
        raise PrivateOCRError('existing global models defer this private inference proof')


@contextmanager
def serve(stage, guard):
    stage, model_bytes = verify_stage(stage)
    guard_result = subprocess.run(['bash',str(guard),'--guard'], stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=15)
    if guard_result.returncode:
        raise PrivateOCRError('host resource guard deferred the private backend')
    require_global_idle()
    before = resources(); time.sleep(1); after = resources()
    check_resources(before,after,model_bytes)
    fd = os.open(stage/'ocr-session.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    with os.fdopen(fd,'r+b') as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise PrivateOCRError('unsafe private backend session lock')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        env = {k:v for k,v in os.environ.items() if k in ('PATH','HOME','USER','LOGNAME','LANG','LC_ALL')}
        for name in ('tmp','cache'):
            (stage/name).mkdir(mode=0o700,exist_ok=True)
            pinned.real_directory(stage/name,private=True)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        env.update(OLLAMA_HOST='127.0.0.1:'+str(port),OLLAMA_MODELS=str(stage/'models'),
                   OLLAMA_NO_CLOUD='1',OLLAMA_NOPRUNE='1',OLLAMA_MAX_LOADED_MODELS='1',
                   OLLAMA_NUM_PARALLEL='1',OLLAMA_KEEP_ALIVE='0',OLLAMA_CONTEXT_LENGTH='2048',
                   OLLAMA_MAX_QUEUE='1',TMPDIR=str(stage/'tmp'),XDG_CACHE_HOME=str(stage/'cache'))
        profile=sandbox_profile(stage)
        check_sandbox(stage,profile,env)
        with private_output(stage/'inference-server.log') as log:
            child=subprocess.Popen(['/usr/bin/sandbox-exec','-p',profile,
                                    str(stage/'runtime/ollama'),'serve'],env=env,
                                   stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                   start_new_session=True,umask=0o077)
        stop=threading.Event()
        interrupted={}
        monitor=None
        signal_lock=threading.Lock()
        try:
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                # Do not poll/wait here: even an exited startup leader must
                # reserve its PID until its remaining descendants are killed.
                try:
                    if pinned.metadata(port,'/api/version').get('version')!='0.40.6':
                        raise PrivateOCRError('private backend version mismatch')
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise PrivateOCRError('private backend readiness deadline')
            listener=subprocess.run(['/usr/sbin/lsof','-a','-p',str(child.pid),'-iTCP:'+str(port),
                                     '-sTCP:LISTEN','-nP'],capture_output=True,text=True,timeout=5)
            if listener.returncode or '127.0.0.1:'+str(port) not in listener.stdout or '*:'+str(port) in listener.stdout:
                raise PrivateOCRError('private loopback listener identity mismatch')
            if pinned.metadata(port,'/api/ps').get('models')!=[]:
                raise PrivateOCRError('private backend unexpectedly loaded weights')
            require_global_idle()
            check_resources(after,resources(),model_bytes)
            expires=time.monotonic()+150
            def watch():
                while not stop.wait(.5):
                    try:
                        reason=pressure_reason(resources(),after['swapouts'])
                        if reason is None:
                            require_global_idle()
                        if time.monotonic()>=expires:
                            reason='proof_time_budget'
                    except Exception:
                        reason='resource_probe_failed'
                    if reason:
                        interrupted['reason']=reason
                        # Do not reap here; the reserved Popen PID protects the
                        # owned process-group identity until final cleanup.
                        interrupt_owned_group(child,stop,signal_lock)
                        return
            monitor=threading.Thread(target=watch,daemon=True)
            monitor.start()
            yield dict(endpoint='http://127.0.0.1:'+str(port),pid=child.pid,
                       model_reference='sha256-'+pinned.MANIFEST_SHA,interrupted=interrupted,
                       recheck_global_idle=require_global_idle)
            if interrupted:
                raise PrivateOCRError('private proof interrupted: '+interrupted['reason'])
        finally:
            cleanup_owned_session(child,stop,signal_lock,monitor)
