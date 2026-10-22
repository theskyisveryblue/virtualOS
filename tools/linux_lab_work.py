"""Continue the prepared Linux persistence test behind fresh resource gates."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

from cloud_disk_archive_delegate import paths
from cloud_disk_archive_session import safe_file, write_json

TOOLS = Path(__file__).resolve().parent
REMOTE = '/Users/ai/vos-fleet'
ARTIFACTS = REMOTE + '/linux/ext4-6.6.142-20261017'


def expected_artifacts():
    row = json.loads((TOOLS / 'experiments/linux-ext4-artifact-receipt.json').read_text())
    expected = {key: row[key] for key in ('kernel_sha256', 'initramfs_sha256', 'package_sha256')}
    if (any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in expected.values())
            or row.get('artifact_check', {}).get('linux_artifacts') != 'ready'):
        raise ValueError('prepared Linux artifact pins required')
    return expected


def boot_command(expected, *, artifacts=None, lab=None, maintained=False):
    code = '''import fcntl,hashlib,json,os,pathlib,stat,subprocess
os.umask(0o077)
root=pathlib.Path(ROOT)
receipt=json.loads((root/RECEIPT).read_text())
assert all(receipt[k]==v for k,v in EXPECTED.items()), 'artifact receipt changed'
for name,key in [('Image','kernel_sha256'),('initramfs','initramfs_sha256')]:
 p=root/name
 info=p.lstat()
 assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid(), 'unsafe boot artifact'
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1048576),b''):h.update(block)
 assert h.hexdigest()==EXPECTED[key], 'boot artifact differs from pin'
env=dict(os.environ,VOS_LINUX_KERNEL=str(root/'Image'),VOS_LINUX_INITRD=str(root/'initramfs'))
if MAINTAINED:
 sys_path=str(pathlib.Path(LAB).parent)
 import sys
 sys.path.insert(0,sys_path)
 from linux_maintained_profile import check
 check(root/'profile.json')
 env.update(VOS_LINUX_MAINTAINED_PROFILE=str(root/'profile.json'),VOS_LINUX_HOME=str(root),VOS_LINUX_DISK=SOURCE)
lock_path=root.parent/'persistence-test.lock'
if lock_path.exists() or lock_path.is_symlink():
 info=lock_path.lstat()
 assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1 and not info.st_mode&0o077
with lock_path.open('a') as lock:
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:raise SystemExit(75)
 # The lab shell and its guest inherit the lease through an SSH disconnect.
 raise SystemExit(subprocess.call(['bash',LAB],env=env,pass_fds=(lock.fileno(),)))
'''
    code = ('ROOT=' + repr(artifacts or ARTIFACTS) + '\nEXPECTED=' + repr(expected) + '\nLAB='
            + repr(lab or REMOTE + '/tools/linux-persist-lab.sh')
            + '\nRECEIPT=' + repr('profile.json' if maintained else 'ready.json')
            + '\nMAINTAINED=' + repr(maintained) + '\nSOURCE=' + repr(REMOTE + '/linux/inner-root.img') + '\n' + code)
    return 'python3 -c ' + shlex.quote(code)


def step(execute, expected, *, phase=lambda row: None, boot=boot_command):
    check = execute('python3 ' + REMOTE + '/tools/linux_lab_resources.py check', 40)
    if check.returncode == 75:
        try:
            report=json.loads(getattr(check,'stdout',''))
        except (ValueError,TypeError):
            report={}
        safe={}
        for key in ('disk_free_kib','swap_used_mib','memory_free_percent','load_one','cpu_count'):
            value=report.get(key)
            if type(value) in (int,float) and math.isfinite(value):safe[key]=value
        reasons=report.get('reasons',[])
        allowed={'disk_headroom','swap_pressure','memory_headroom','cpu_headroom','resource_measurement_unavailable'}
        safe['reasons']=[reason for reason in reasons if isinstance(reason,str) and reason in allowed] if isinstance(reasons,list) else []
        return {'state': 'waiting_for_resources', 'resource_gate': safe}
    if check.returncode:
        return {'state': 'waiting_for_connection' if check.returncode == 255 else 'requires_attention',
                'stage': 'resource_check', 'returncode': check.returncode}
    phase({'state': 'running_linux_persistence_test'})
    result = execute(boot(expected), 1800)
    if result.returncode in (75, 255):
        return {'state': 'waiting_for_resources' if result.returncode == 75 else 'waiting_for_connection'}
    if result.returncode:
        return {'state': 'requires_attention', 'stage': 'linux_test', 'returncode': result.returncode}
    marker = '== linux-persist-lab result =='
    summary = result.stdout.split(marker, 1)[-1].lstrip()
    if marker not in result.stdout:
        raise ValueError('Linux test did not publish a result')
    proof, _ = json.JSONDecoder().raw_decode(summary)
    if any(proof.get(key) is not True for key in
           ('formatted', 'rootfs_laid_down', 'init_installed', 'root_login_locked', 'disk_rooted_boot_1', 'disk_rooted_boot_2', 'persistent')):
        raise ValueError('Linux persistence proof is incomplete')
    return {'state': 'verified', 'proof': proof, 'production_guests_restarted': False}


def main():
    os.umask(0o077)
    directory = paths()
    for name in ('linux-work.lock', 'linux-work-launch.lock', 'linux-work.log',
                 'linux-work.json', 'linux-work.json.next', 'linux-work-console.txt'):
        safe_file(directory / name)
    expected = expected_artifacts()
    fingerprint = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    status_path = directory / 'linux-work.json'
    def state(row):
        row.update(pid=os.getpid(), updated_at=time.time(), artifact_fingerprint=fingerprint)
        write_json(status_path, row)
        print(json.dumps(row, sort_keys=True), flush=True)
    if len(sys.argv) == 2 and sys.argv[1] == 'start':
        with (directory / 'linux-work-launch.lock').open('a') as launch:
            fcntl.flock(launch, fcntl.LOCK_EX)
            with (directory / 'linux-work.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print('linux_work=running'); return
                if status_path.exists():
                    previous = json.loads(status_path.read_text())
                    if previous.get('state') == 'verified' and previous.get('artifact_fingerprint') == fingerprint:
                        print('linux_work=verified'); return
                env = {k: v for k, v in os.environ.items() if not
                    (k.startswith(('AWS_', 'CF_', 'CLOUDFLARE_')) or any(word in k.upper()
                     for word in ('SECRET', 'TOKEN', 'PASSWORD', 'API_KEY')))}
                with (directory / 'linux-work.log').open('a') as log:
                    child = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()),
                        'run', str(lock.fileno())], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        env=env, start_new_session=True, pass_fds=(lock.fileno(),))
                print('linux_work_started=' + str(child.pid))
        return
    if len(sys.argv) != 3 or sys.argv[1] != 'run':
        raise ValueError('invalid Linux continuation operation')
    lock = os.fdopen(int(sys.argv[2]), 'a')
    ssh_config = Path.home() / 'Documents/GitHub/realestate-machine/.local/mini-remote-20260915/ssh_config'
    ssh = ['ssh', '-F', str(ssh_config), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
           '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
    from cloud_disk_connections import control_options
    ssh += control_options(directory) + ['mac-mini-cloudflare']
    def execute(command, timeout):
        result = subprocess.run(ssh + [command], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout)
        if command.startswith('python3 -c '):
            safe_file(directory / 'linux-work-console.txt').write_text(result.stdout + result.stderr)
        return result
    try:
        while True:
            state({'state': 'checking_resources'})
            try:
                result = step(execute, expected, phase=state)
            except (OSError, subprocess.TimeoutExpired):
                result = {'state': 'waiting_for_connection'}
            state(result)
            if result['state'] not in ('waiting_for_resources', 'waiting_for_connection'):
                return
            time.sleep(60)
    except Exception as error:
        state({'state': 'requires_attention', 'error': type(error).__name__})
    finally:
        lock.close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({'linux_work': 'requires_attention', 'error': type(error).__name__}))
        raise SystemExit(1)
