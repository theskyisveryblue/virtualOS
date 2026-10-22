"""Resource-gated private Alpine 3.24 proof; baseline guests stay selected."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from cloud_disk_archive_delegate import paths
from cloud_disk_archive_session import safe_file,write_json
from cloud_disk_connections import control_options
from linux_lab_work import boot_command,step
from linux_maintained_profile import check

TOOLS=Path(__file__).resolve().parent
RUNTIME=('linux-persist-lab.sh','linux_lab_resources.py','computer-resources.sh',
         'linux_lab_storage.py','linux_maintained_profile.py','linux_prepare_ext4.py',
         'linux_boot_artifacts.py','linux_lab_console.py')


def plan(profile_path):
    profile=check(profile_path)
    runtime={name:hashlib.sha256((TOOLS/name).read_bytes()).hexdigest() for name in RUNTIME}
    fingerprint=hashlib.sha256(json.dumps(dict(profile=profile,runtime=runtime),sort_keys=True).encode()).hexdigest()
    remote='/Users/ai/vos-fleet/linux/maintained-3.24-'+fingerprint[:16]
    return profile,runtime,fingerprint,remote


def main():
    os.umask(0o077)
    if len(sys.argv) not in (3,4) or sys.argv[1] not in ('start','run'):
        raise ValueError('internal maintained start/run PROFILE required')
    profile_path=Path(sys.argv[2]).resolve()
    profile,runtime,fingerprint,remote=plan(profile_path)
    directory=paths()
    for name in ('linux-maintained.lock','linux-maintained-launch.lock','linux-maintained.log',
                 'linux-maintained.json','linux-maintained.json.next','linux-maintained-console.txt'):
        safe_file(directory/name)
    status_path=directory/'linux-maintained.json'
    def state(row):
        row.update(pid=os.getpid(),updated_at=time.time(),profile_fingerprint=fingerprint,remote_directory=remote)
        write_json(status_path,row)
        print(json.dumps(row,sort_keys=True),flush=True)
    if sys.argv[1]=='start':
        with (directory/'linux-maintained-launch.lock').open('a') as launch:
            fcntl.flock(launch,fcntl.LOCK_EX)
            with (directory/'linux-maintained.lock').open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:print('maintained_linux=running');return
                if status_path.exists():
                    previous=json.loads(status_path.read_text())
                    if previous.get('state')=='verified' and previous.get('profile_fingerprint')==fingerprint:
                        print('maintained_linux=verified');return
                env={k:v for k,v in os.environ.items() if not (k.startswith(('AWS_','CF_','CLOUDFLARE_'))
                     or any(word in k.upper() for word in ('SECRET','TOKEN','PASSWORD','API_KEY')))}
                with (directory/'linux-maintained.log').open('a') as log:
                    child=subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve()),'run',str(profile_path),str(lock.fileno())],
                        stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=env,start_new_session=True,pass_fds=(lock.fileno(),))
                print('maintained_linux_started='+str(child.pid))
        return
    lock=os.fdopen(int(sys.argv[3]),'a')
    config=Path.home()/'Documents/GitHub/realestate-machine/.local/mini-remote-20260915/ssh_config'
    options=['-F',str(config),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
             '-o','ConnectTimeout=15','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2']+control_options(directory)
    ssh=['ssh']+options+['mac-mini-cloudflare']
    local=profile_path.parent
    expected={key:profile[key] for key in ('kernel_sha256','initramfs_sha256','rootfs_profile_sha256')}
    manifest={name:hashlib.sha256((local/name).read_bytes()).hexdigest() for name in ('Image','initramfs','profile.json')}
    manifest.update({'runtime/tools/'+name:digest for name,digest in runtime.items()})
    test_command=None
    staged=False
    def execute(command,timeout):
        result=subprocess.run(ssh+[command],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout)
        if command==test_command:
            safe_file(directory/'linux-maintained-console.txt').write_text(result.stdout+result.stderr)
        return result
    def stage_and_boot(_):
        nonlocal staged,test_command
        if not staged:
            state(dict(state='staging_private_maintained_profile'))
            prepare='''import os,pathlib,stat
os.umask(0o077)
root=pathlib.Path(ROOT)
for path in (root,root/'runtime',root/'runtime/tools',root/'runtime/tools/fleet'):
 if path.exists() or path.is_symlink():
  info=path.lstat()
  assert stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077
 else:path.mkdir(mode=0o700)
binary=root/'runtime/tools/fleet/linux-guest'
target='/Users/ai/vos-fleet/tools/fleet/linux-guest'
if binary.is_symlink():assert os.readlink(binary)==target
elif binary.exists():raise ValueError('unexpected maintained binary')
else:binary.symlink_to(target)
'''
            command='python3 -c '+shlex.quote('ROOT='+repr(remote)+'\n'+prepare)
            prepared=execute(command,60)
            if prepared.returncode:raise subprocess.CalledProcessError(prepared.returncode, 'private-profile-prepare')
            subprocess.run(['scp']+options+[str(local/name) for name in ('Image','initramfs','profile.json')]
                           +['mac-mini-cloudflare:'+remote+'/'],check=True,timeout=120,capture_output=True)
            subprocess.run(['scp']+options+[str(TOOLS/name) for name in RUNTIME]
                           +['mac-mini-cloudflare:'+remote+'/runtime/tools/'],check=True,timeout=60,capture_output=True)
            verify='''import hashlib,pathlib,stat
root=pathlib.Path(ROOT)
for name,digest in EXPECTED.items():
 path=root/name
 info=path.lstat()
 assert stat.S_ISREG(info.st_mode) and info.st_uid==__import__('os').getuid() and info.st_nlink==1
 assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
 path.chmod(0o600)
'''
            verified=execute('python3 -c '+shlex.quote('ROOT='+repr(remote)+'\nEXPECTED='+repr(manifest)+'\n'+verify),60)
            if verified.returncode:raise ValueError('maintained private runtime hash verification failed')
            staged=True
        state(dict(state='running_maintained_linux_persistence_test'))
        test_command=boot_command(expected,artifacts=remote,lab=remote+'/runtime/tools/linux-persist-lab.sh',maintained=True)
        return test_command
    try:
        deadline=time.monotonic()+8*3600
        while time.monotonic()<deadline:
            state(dict(state='checking_resources'))
            try:
                result=step(execute,expected,phase=state,boot=stage_and_boot)
            except (OSError,subprocess.TimeoutExpired):result=dict(state='waiting_for_connection')
            except subprocess.CalledProcessError as error:
                result=dict(state='waiting_for_connection' if error.returncode==255 else 'requires_attention',returncode=error.returncode)
            if result.get('state')=='verified' and result.get('proof',{}).get('maintained_userland') is not True:
                raise ValueError('maintained userland was not independently confirmed in both disk boots')
            state(result)
            if result['state'] not in ('waiting_for_resources','waiting_for_connection'):return
            time.sleep(60)
        state(dict(state='waiting_window_elapsed'))
    except Exception as error:
        state(dict(state='requires_attention',error=type(error).__name__))
        raise
    finally:lock.close()


if __name__=='__main__':main()
