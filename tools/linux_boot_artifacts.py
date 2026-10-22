"""Read-only admission check for an ext4-rooted Linux persistence experiment."""
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


def tar_read(initrd, *args):
    result = subprocess.run(['tar', *args, str(initrd)] if args == ('-tf',) else
                            ['tar', '-xOf', str(initrd), *args],
                            capture_output=True, timeout=20, check=True)
    if len(result.stdout) > 4 * 1024**2:
        raise ValueError('initramfs metadata exceeds audit bound')
    return result.stdout.decode('utf-8', errors='strict')


def audit(kernel, initrd):
    for path in (kernel, initrd):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 128 * 1024**2:
            raise ValueError('bounded regular kernel and initramfs files required')
    version = re.search(rb'Linux version ([A-Za-z0-9.+_-]+) ', kernel.read_bytes())
    if version is None:
        raise ValueError('kernel release could not be identified')
    release = version[1].decode('ascii')
    listed = tar_read(initrd, '-tf').splitlines()
    names = {}
    for original in listed:
        name = original.removeprefix('./').rstrip('/')
        if name in ('', '.'):
            continue
        if name.startswith('/') or '..' in PurePosixPath(name).parts:
            raise ValueError('unsafe initramfs member name')
        names[name] = original
    releases = sorted({p.split('/')[2] for p in names if p.startswith('lib/modules/') and len(p.split('/')) > 3})
    if releases != [release]:
        return {'linux_artifacts': 'refused', 'reason': 'kernel_module_release_mismatch',
                'kernel_release': release, 'module_releases': releases}
    prefix = 'lib/modules/' + release + '/'
    dep_name = prefix + 'modules.dep'
    builtin_name = prefix + 'modules.builtin'
    dependencies = {}
    if dep_name in names:
        for line in tar_read(initrd, names[dep_name]).splitlines():
            module, separator, deps = line.partition(':')
            if separator:
                dependencies[module] = deps.split()
    builtins = set(tar_read(initrd, names[builtin_name]).splitlines()) if builtin_name in names else set()
    ext4 = next((name for name in dependencies if re.fullmatch(r'kernel/fs/ext4/ext4\.ko(?:\.(?:gz|xz|zst))?', name)), None)
    if 'kernel/fs/ext4/ext4.ko' in builtins:
        pending = []
    elif ext4:
        pending = [ext4]
    else:
        return {'linux_artifacts': 'refused', 'reason': 'ext4_driver_missing', 'kernel_release': release}
    visited = set()
    missing = []
    while pending:
        module = pending.pop()
        if module in visited or module in builtins:
            continue
        visited.add(module)
        if prefix + module not in names or module not in dependencies:
            missing.append(module)
            continue
        pending.extend(dependencies[module])
    return {'linux_artifacts': 'refused' if missing else 'ready',
            'reason': 'ext4_dependency_missing' if missing else 'matching_ext4_driver_available',
            'kernel_release': release, 'modules_checked': len(visited), 'missing': sorted(missing),
            'boot_verified': False}


if __name__ == '__main__':
    try:
        result = audit(Path(sys.argv[1]), Path(sys.argv[2]))
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(0 if result['linux_artifacts'] == 'ready' else 1)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({'linux_artifacts': 'refused', 'error': type(error).__name__}))
        raise SystemExit(1)
