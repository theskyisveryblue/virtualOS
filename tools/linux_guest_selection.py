"""Validate an explicit future Linux selection; never activate or boot it."""
from contextlib import contextmanager
import hashlib
import errno
import json
import os
from pathlib import Path
import re
import stat
import sys

from linux_maintained_profile import check

SCHEMA = 'virtualos-linux-selection/v1'
PINS = {
    'kernel_sha256': '1993474a172566b1573234900ff611c9fa4307b9398742c4c210b1e9664d6e56',
    'initramfs_sha256': '30fd14142dcbd44c149a7e798551481ae335174c160c2f6605f82d0b2add9b3f',
    'rootfs_profile_sha256': '08a94fc9438bd19e03a97dfecb8ae4df69654339dc91800f25a3238aba3a1797',
}
DISK_SHA256 = '8f18656717e613bb4a02433d9b44478f99373c2ae5ca653c65c4cec388673c5e'
DISK_BYTES = 268435456


def absolute(value):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('selection requires a literal absolute file path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('selection paths must be absolute without parent traversal')
    return path


@contextmanager
def owned_file(path, *, private=True, maximum=1024**3):
    path = absolute(str(path))
    if private:
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError('selection file parent must be private, owned, and unlinked')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & (0o077 if private else 0o022) or not 0 < info.st_size <= maximum):
            raise ValueError('selection file must be bounded, owned, regular, and safely permissioned')
        yield stream, info


def file_hash(path, **options):
    with owned_file(path, **options) as (stream, info):
        hasher = hashlib.sha256()
        remaining = info.st_size
        while remaining:
            block = stream.read(min(1024**2, remaining))
            if not block:
                raise ValueError('selection file shortened while its pin was checked')
            hasher.update(block)
            remaining -= len(block)
        if stream.read(1):
            raise ValueError('selection file grew while its pin was checked')
        after = os.fstat(stream.fileno())
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('selection file changed while its pin was checked')
        return hasher.hexdigest(), info


def unique_object(pairs):
    row = {}
    for key, value in pairs:
        if key in row:
            raise ValueError('duplicate selection field refused')
        row[key] = value
    return row


def read_json(path, *, private=True):
    with owned_file(path, private=private, maximum=65536) as (stream, _):
        body = stream.read(65537)
    if len(body) > 65536:
        raise ValueError('selection JSON exceeds bound')
    result = json.loads(body, object_pairs_hook=unique_object)
    if not isinstance(result, dict):
        raise ValueError('selection JSON object required')
    return result


def pinned_reference(row, *, private=True, maximum=1024**3):
    if not isinstance(row, dict) or set(row) != {'path', 'sha256'} or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('sha256'))):
        raise ValueError('explicit file path and SHA-256 pin required')
    path = absolute(row['path'])
    digest, info = file_hash(path, private=private, maximum=maximum)
    if digest != row['sha256']:
        raise ValueError('selection artifact SHA-256 pin mismatch')
    return path, info


def selection(config_path):
    config = read_json(absolute(str(config_path)))
    if config.get('schema') != SCHEMA:
        raise ValueError('unsupported Linux selection schema')
    mode = config.get('mode')
    if mode == 'legacy':
        if set(config) != {'schema', 'mode', 'kernel', 'initramfs', 'disk'}:
            raise ValueError('legacy selection requires explicit pinned artifacts')
        kernel, _ = pinned_reference(config['kernel'], maximum=128*1024**2)
        initrd, _ = pinned_reference(config['initramfs'], maximum=128*1024**2)
        disk, _ = pinned_reference(config['disk'])
        cmdline = 'console=hvc0'
        budget = (4, 3)  # Existing default launcher; this command never boots it.
    elif mode == 'maintained':
        if set(config) != {'schema', 'mode', 'profile', 'receipt', 'proof_disk', 'runtime_disk'}:
            raise ValueError('maintained selection requires profile, receipt, and separate runtime disk')
        profile_path = absolute(config['profile'])
        # Check secure ownership before the existing signed-profile validator.
        read_json(profile_path)
        profile = check(profile_path)
        if any(profile.get(key) != pin for key, pin in PINS.items()):
            raise ValueError('maintained profile differs from the verified two-boot pins')
        receipt_path, _ = pinned_reference(config['receipt'], private=False, maximum=65536)
        receipt = read_json(receipt_path, private=False)
        files = receipt.get('files')
        root_disk = files.get('root.img') if isinstance(files, dict) else None
        if (receipt.get('schema') != 'virtualos-maintained-linux-persistence-receipt/v1'
                or any(receipt.get(key) != pin for key, pin in PINS.items())
                or any(receipt.get(key) is not True for key in (
                    'disk_rooted_boot_1', 'disk_rooted_boot_2', 'maintained_userland', 'root_login_locked', 'persistent'))
                or not isinstance(root_disk, dict) or root_disk.get('sha256') != DISK_SHA256
                or root_disk.get('bytes') != DISK_BYTES):
            raise ValueError('maintained persistence proof receipt is incomplete or mismatched')
        kernel, initrd = profile_path.parent/'Image', profile_path.parent/'initramfs'
        for path, key in ((kernel, 'kernel_sha256'), (initrd, 'initramfs_sha256')):
            if file_hash(path, maximum=128*1024**2)[0] != PINS[key]:
                raise ValueError('maintained artifact pin changed')
        source = absolute(config['proof_disk'])
        disk = absolute(config['runtime_disk'])
        proof_hash, proof_info = file_hash(source)
        runtime_hash, runtime_info = file_hash(disk)
        if (proof_info.st_dev, proof_info.st_ino) == (runtime_info.st_dev, runtime_info.st_ino):
            raise ValueError('runtime disk must be a separate private copy of the retained proof')
        if (proof_hash != DISK_SHA256 or runtime_hash != DISK_SHA256
                or proof_info.st_size != DISK_BYTES or runtime_info.st_size != DISK_BYTES):
            raise ValueError('prepared runtime disk must match the sealed maintained proof pin')
        cmdline, budget = 'console=hvc0 root=/dev/vda init=/init rw', (1, 1)
    else:
        raise ValueError('select maintained or legacy explicitly; automatic fallback is refused')
    return dict(schema=SCHEMA, mode=mode, state='prepared_selection_verified',
                activation_required=True, default_selection_changed=False, disk_mutation_performed=False,
                verification_scope='initial artifacts and disk only; no running-guest or rollout claim',
                arguments=['--kernel', str(kernel), '--initrd', str(initrd), '--disk', str(disk),
                           '--cmdline', cmdline, '--cpu', str(budget[0]), '--memory', str(budget[1])])


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('explicit private selection JSON path required')
        print(json.dumps(selection(Path(sys.argv[1])), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as failure:
        # These messages describe policy, never secret file contents.
        if isinstance(failure, FileNotFoundError):
            message = 'selection file or pinned artifact is missing; no fallback was applied'
        elif isinstance(failure, OSError) and failure.errno == errno.ELOOP:
            message = 'selection symlink refused'
        else:
            message = str(failure) if isinstance(failure, ValueError) else type(failure).__name__
        print(json.dumps({'linux_selection': 'refused', 'reason': message}))
        raise SystemExit(2)
