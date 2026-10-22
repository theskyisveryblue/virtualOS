"""Retain proof/logs while bounding only private, stopped Linux lab disks."""
import json
import hashlib
import fcntl
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time

SCHEMA = 'virtualos-linux-lab-storage/v1'


def directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private owned real lab directory required')


def state_path(output):
    path = output / 'lab-state.json'
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 8192):
            raise ValueError('unsafe lab state file')
    return path


def write_state(output, row):
    path = state_path(output)
    temporary = output / 'lab-state.json.next'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(row, stream, sort_keys=True)
        stream.write('\n')
    temporary.replace(path)


def validate(output, parent):
    directory(parent)
    directory(output)
    if output.parent != parent or not re.fullmatch(r'run\.[A-Za-z0-9]{8}', output.name):
        raise ValueError('only the default private persistence run directory is eligible')


def initialize(output, source, parent):
    validate(output, parent)
    if state_path(output).exists():
        raise ValueError('refusing to replace prior lab storage ownership')
    write_state(output, dict(schema=SCHEMA, source=str(source.resolve()), state='prepared', created_at=time.time()))


def no_open_files(path):
    checked = subprocess.run(['lsof', '-nP', str(path)], capture_output=True, timeout=15)
    return checked.returncode == 1 and not checked.stdout and not checked.stderr


def discard(output, parent, *, probe=no_open_files):
    validate(output, parent)
    row = json.loads(state_path(output).read_text())
    if row.get('schema') != SCHEMA or row.get('state') not in ('resource_aborted', 'failed'):
        raise ValueError('only explicitly failed/aborted private lab disks are eligible')
    disk = output / 'root.img'
    if not disk.exists() and not disk.is_symlink():
        return False
    if disk.resolve() == Path(row['source']).resolve():
        raise ValueError('source disk must never be removed')
    info = disk.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or info.st_nlink != 1 or not 0 < info.st_size <= 1024**3):
        raise ValueError('bounded private unlinked regular test disk required')
    source = Path(row['source'])
    if source.exists() and (source.stat().st_dev, source.stat().st_ino) == (info.st_dev, info.st_ino):
        raise ValueError('source inode must never be removed')
    if not probe(disk):
        raise ValueError('test disk open-file check did not prove it unused')
    descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.stat('root.img', dir_fd=descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_nlink, current.st_size) != (
                info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink, info.st_size):
            raise ValueError('test disk changed during unused-file check')
        os.unlink('root.img', dir_fd=descriptor)
    finally:
        os.close(descriptor)
    row.update(disk_retained=False, disk_removed_at=time.time())
    write_state(output, row)
    return True


def sparsify(output, parent, expected_sha256, *, probe=no_open_files):
    """Replace only a failed private lab image with an exact sparse copy."""
    validate(output, parent)
    row = json.loads(state_path(output).read_text())
    if row.get('schema') != SCHEMA or row.get('state') not in ('failed', 'resource_aborted'):
        raise ValueError('only a failed or aborted private lab image can be sparsified')
    if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('reviewed image SHA-256 required')
    disk = output / 'root.img'
    before = disk.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or before.st_mode & 0o077 or before.st_nlink != 1 or not 0 < before.st_size <= 1024**3
            or disk.resolve() == Path(row['source']).resolve()):
        raise ValueError('bounded private lab image required')
    if not probe(disk):
        raise ValueError('image open-file check did not prove it unused')
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid,
                info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)
    if identity(disk.lstat()) != identity(before):
        raise ValueError('image changed during admission')
    descriptor, name = tempfile.mkstemp(prefix='.sparse-root-', dir=output)
    temporary = Path(name)
    try:
        zero_ranges = []
        with os.fdopen(descriptor, 'wb') as destination:
            # Establish final EOF before writes; explicit hole punching below
            # handles APFS allocation of skipped ranges.
            destination.truncate(before.st_size)
            destination.seek(0)
            fd = os.open(disk, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                if identity(os.fstat(source.fileno())) != identity(before):
                    raise ValueError('image descriptor changed')
                checksum = hashlib.sha256()
                remaining = before.st_size
                while remaining:
                    data = source.read(min(65536, remaining))
                    if not data:
                        raise ValueError('image truncated')
                    checksum.update(data)
                    if any(data): destination.write(data)
                    else:
                        start = before.st_size - remaining
                        if zero_ranges and zero_ranges[-1][0] + zero_ranges[-1][1] == start:
                            zero_ranges[-1] = (zero_ranges[-1][0], zero_ranges[-1][1] + len(data))
                        else:
                            zero_ranges.append((start, len(data)))
                        destination.seek(len(data), os.SEEK_CUR)
                    remaining -= len(data)
                if source.read(1) or identity(os.fstat(source.fileno())) != identity(before):
                    raise ValueError('image changed while copying')
            destination.truncate(before.st_size)
            destination.flush(); os.fsync(destination.fileno())
        if sys.platform == 'darwin':
            # Darwin SDK sys/fcntl.h: F_PUNCHHOLE=99, fpunchhole_t contains
            # two uint32 fields followed by the 64-bit offset and length.
            fd = os.open(temporary, os.O_RDWR | os.O_NOFOLLOW)
            try:
                alignment = os.fstat(fd).st_blksize
                for start, length in zero_ranges:
                    length = length // alignment * alignment
                    if length:
                        fcntl.fcntl(fd, 99, struct.pack('IIqq', 0, 0, start, length))
                os.fsync(fd)
            finally:
                os.close(fd)
        if checksum.hexdigest() != expected_sha256:
            raise ValueError('image does not match reviewed SHA-256')
        copied = hashlib.sha256()
        with temporary.open('rb') as candidate:
            for data in iter(lambda: candidate.read(1024**2), b''): copied.update(data)
        if copied.hexdigest() != expected_sha256:
            raise ValueError('sparse image readback failed')
        after = temporary.stat()
        if not probe(disk) or identity(disk.lstat()) != identity(before):
            raise ValueError('image became active or changed before publication')
        if after.st_blocks >= before.st_blocks:
            return dict(sparsified=False, reason='no_physical_saving', original_retained=True)
        os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(temporary, disk)
        folder = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(folder)
        finally: os.close(folder)
        return dict(sparsified=True, sha256=expected_sha256, logical_bytes=before.st_size,
                    allocated_bytes_before=before.st_blocks * 512,
                    allocated_bytes_after=after.st_blocks * 512,
                    allocated_bytes_reclaimed=(before.st_blocks-after.st_blocks) * 512,
                    byte_identity_verified=True, diagnostic_logs_preserved=True)
    finally:
        temporary.unlink(missing_ok=True)


def finalize(output, parent, status, *, probe=no_open_files):
    validate(output, parent)
    row = json.loads(state_path(output).read_text())
    if row.get('schema') != SCHEMA:
        raise ValueError('lab ownership metadata required')
    row.update(state='verified' if status == 0 else ('resource_aborted' if status == 75 else 'failed'),
               exit_status=status, finished_at=time.time(), disk_retained=(output / 'root.img').exists())
    write_state(output, row)
    removed = 0
    if status == 75:
        removed += discard(output, parent, probe=probe)
    # Keep one recent failed test disk for diagnosis. Old unlabelled runs and
    # successful proof disks are never candidates for automatic reclamation.
    failed = []
    for candidate in parent.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if not re.fullmatch(r'run\.[A-Za-z0-9]{8}', candidate.name):
            continue
        directory(candidate)
        path = state_path(candidate)
        if not path.exists():
            continue
        previous = json.loads(path.read_text())
        if (previous.get('schema') == SCHEMA and previous.get('state') == 'failed'
                and (candidate / 'root.img').exists()):
            failed.append((previous.get('finished_at', 0), candidate))
    for _, candidate in sorted(failed, reverse=True)[1:]:
        removed += discard(candidate, parent, probe=probe)
    return dict(linux_lab_storage='finalized', removed_private_disks=removed,
                current_disk_retained=(output / 'root.img').exists(), logs_preserved=True)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        if len(sys.argv) != 5:
            raise ValueError('operation/output/source-or-status/private-parent required')
        operation, output, value, parent = sys.argv[1:]
        if operation == 'initialize':
            initialize(Path(output), Path(value), Path(parent))
        elif operation == 'finalize':
            print(json.dumps(finalize(Path(output), Path(parent), int(value)), sort_keys=True))
        else:
            raise ValueError('invalid lab storage operation')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit('linux_storage=refused: ' + type(error).__name__)
