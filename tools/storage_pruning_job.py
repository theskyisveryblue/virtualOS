"""Fixed background storage job, dispatched by the existing shell supervisor."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from cloud_disk_archive_delegate import paths
from cloud_disk_archive_session import safe_file, write_json
from cloud_disk_connections import control_options

TOOLS = Path(__file__).resolve().parent
SOURCE_NAMES = ('cloud_disk.py', 'compressed_delta.py', 'overlay_compact.py', 'overlay_pruning_audit.py')


def should_start(state, now):
    return (state.get('state') not in ('verified', 'requires_attention')
            and now - state.get('updated_at', 0) >= 300)


def eligible(directory):
    from agent_control import Control
    if not (directory / 'agent-control/agent-control.json').exists():
        return False
    snapshot = Control(directory / 'agent-control', TOOLS.parent).snapshot()
    task = next((row for row in snapshot['tasks'] if row['id'] == 'overlay_pruning_audit'), None)
    return (task is not None and task['status'] != 'done'
            and set(task['block_reasons']) <= {'resources'}
            and snapshot.get('leases', {}).get('worker') is None)


def remote_payload():
    sources = {name: (TOOLS / name).read_text() for name in SOURCE_NAMES}
    program = (TOOLS / 'storage_pruning_remote.py').read_text()
    # JSON string literals are Python-compatible for these string-only values.
    return (program + '\nresult = run(json.loads(' + repr(json.dumps(sources)) + '))\n'
            + 'result["remote_program_sha256"] = '
            + repr(hashlib.sha256(program.encode()).hexdigest()) + '\nprint(json.dumps(result))\n')


def accept(directory, result):
    """Import only independently pinned success, never a running/waiting job."""
    from agent_control import Control
    if result.get('state') != 'verified' or not eligible(directory):
        return False
    expected = {name: hashlib.sha256((TOOLS / name).read_bytes()).hexdigest() for name in SOURCE_NAMES}
    audit = result.get('audit', {})
    count = audit.get('blocks')
    redundant = audit.get('redundant_blocks')
    if (result.get('source_sha256') != expected or not result.get('temporary_snapshot_released')
            or result.get('remote_program_sha256') != hashlib.sha256(
                (TOOLS / 'storage_pruning_remote.py').read_bytes()).hexdigest()
            or not result.get('original_identity_unchanged')
            or not result.get('protected_processes_unchanged')
            or result.get('guest_services_changed') is not False
            or result.get('audit', {}).get('closed_snapshot') is not True
            or result.get('audit', {}).get('read_only') is not True
            or result.get('audit', {}).get('actual_reclaimed_bytes') != 0
            or type(count) is not int or not 0 <= count <= 32768
            or type(redundant) is not int or not 0 <= redundant <= count):
        raise ValueError('pruning receipt contract mismatch')
    bindings = {'tools/' + name: pin for name, pin in expected.items()}
    for name in ('storage_pruning_job.py', 'storage_pruning_remote.py'):
        bindings['tools/' + name] = hashlib.sha256((TOOLS / name).read_bytes()).hexdigest()
    receipt = safe_file(directory / 'agent-control/storage-pruning-result.json')
    write_json(receipt, dict(result, source_bindings=bindings))
    control = Control(directory / 'agent-control', TOOLS.parent)
    control.set_gate('overlay_pruning_audit', 'resources', True)
    try:
        control.attest('overlay_pruning_audit', str(receipt), hashlib.sha256(receipt.read_bytes()).hexdigest(),
                       bindings=bindings, complete=True)
    except ValueError:
        control.set_gate('overlay_pruning_audit', 'resources', False)
        raise
    return True


def main():
    os.umask(0o077)
    directory = paths()
    for filename in ('storage-pruning.lock', 'storage-pruning.json', 'storage-pruning.json.next',
                     'storage-pruning.log', 'storage-pruning-result.json', 'storage-pruning-result.json.next'):
        safe_file(directory / filename)
    status = directory / 'storage-pruning.json'
    if sys.argv[1:] == ['start']:
        with (directory / 'storage-pruning.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            previous = json.loads(status.read_text()) if status.exists() else {}
            # A completed remote audit may have finished while an agent was busy.
            if previous.get('state') == 'verified':
                accept(directory, previous['result'])
                return
            if not eligible(directory) or not should_start(previous, time.time()):
                return
            with (directory / 'storage-pruning.log').open('a') as log:
                subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()),
                                  'run', str(lock.fileno())], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, pass_fds=(lock.fileno(),), start_new_session=True)
        return
    if len(sys.argv) != 3 or sys.argv[1] != 'run':
        raise ValueError('invalid pruning job operation')
    with os.fdopen(int(sys.argv[2]), 'a'):
        if not eligible(directory):
            return
        write_json(status, dict(state='running', pid=os.getpid(), updated_at=time.time()))
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=8'] + control_options(directory) + [
            '-F', str(Path.home() / 'Documents/GitHub/realestate-machine/.local/mini-remote-20260915/ssh_config'),
            'mac-mini-cloudflare', 'python3 -']
        try:
            row = subprocess.run(command, input=remote_payload(), capture_output=True, text=True, timeout=900)
            if row.returncode:
                # Reconnect only classified transport errors. Integrity/remote failures stop.
                transport = row.returncode == 255
                result = dict(state='waiting_for_connection' if transport else 'requires_attention',
                              error='ssh_transport' if transport else 'remote_audit_failure')
            elif len(row.stdout) > 65536:
                raise ValueError('audit response exceeds bound')
            else:
                result = json.loads(row.stdout)
                if result.get('state') not in ('verified', 'waiting_for_resources'):
                    raise ValueError('invalid remote result')
            write_json(status, dict(state=result['state'], result=result, updated_at=time.time()))
            accept(directory, result)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            write_json(status, dict(state='requires_attention', error=type(error).__name__, updated_at=time.time()))


if __name__ == '__main__':
    main()
