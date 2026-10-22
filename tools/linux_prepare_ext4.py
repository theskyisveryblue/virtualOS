"""Prepare isolated Alpine boot artifacts; never start or modify a running VM.

The package pin was verified against the Alpine RSA signature using the trusted
key from the existing booted initramfs. Its signed .PKGINFO pins the data stream.
"""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import zlib

from linux_boot_artifacts import audit

PACKAGE_SHA256 = '35cb4b105e28bf34ed192a33cf23dc834e90492d9d7e8467a699885cf12b0e89'
RELEASE = '6.6.142-0-virt'
ALPINE_KEY_SHA256 = 'd11f6b21c61b4274e182eb888883a8ba8acdbf820dcc7a6d82a7d9fc2fd2836d'
ALPINE_KEY_NAME = 'alpine-devel@lists.alpinelinux.org-616ae350.rsa.pub'
VZ_BOOT_MODULES = {
    'kernel/drivers/block/virtio_blk.ko', 'kernel/drivers/net/virtio_net.ko',
    'kernel/drivers/char/virtio_console.ko', 'kernel/drivers/virtio/virtio_mmio.ko',
    'kernel/drivers/virtio/virtio_pci.ko', 'kernel/fs/ext4/ext4.ko',
}


def inflate(data, limit):
    decoder = zlib.decompressobj(31)
    expanded = decoder.decompress(data, limit + 1)
    if not decoder.eof or len(expanded) > limit:
        raise ValueError('compressed artifact exceeds bound or is incomplete')
    return expanded, len(data) - len(decoder.unused_data)


def entries(data):
    """Keep original newc records verbatim, including links and device nodes."""
    offset = 0
    while offset + 110 <= len(data):
        start = offset
        header = data[offset:offset + 110]
        if header[:6] not in (b'070701', b'070702'):
            raise ValueError('unsupported initramfs CPIO format')
        fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
        size, namesize = fields[6], fields[11]
        if not 1 <= namesize <= 4096:
            raise ValueError('invalid CPIO name')
        offset += 110
        name_bytes = data[offset:offset + namesize]
        if not name_bytes.endswith(b'\0'):
            raise ValueError('unterminated CPIO name')
        name = name_bytes[:-1].decode('utf-8')
        if name.startswith('/') or '..' in PurePosixPath(name).parts:
            raise ValueError('unsafe CPIO name')
        offset += namesize
        offset += -offset % 4
        body = data[offset:offset + size]
        if len(body) != size:
            raise ValueError('truncated CPIO data')
        if header[:6] == b'070702' and sum(body) & 0xffffffff != fields[12]:
            raise ValueError('CPIO checksum mismatch')
        offset += size
        offset += -offset % 4
        if name == 'TRAILER!!!':
            if any(data[offset:]):
                raise ValueError('unexpected data after CPIO trailer')
            return
        yield name.removeprefix('./'), fields[1], body, data[start:offset]
    raise ValueError('CPIO trailer missing')


def record(name, body, inode, mode=0o100644):
    encoded = name.encode('utf-8') + b'\0'
    fields = [inode, mode, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(encoded), 0]
    header = b'070701' + ''.join(f'{value:08x}' for value in fields).encode('ascii')
    result = header + encoded
    result += b'\0' * (-len(result) % 4)
    result += body
    return result + b'\0' * (-len(result) % 4)


def dependency_closure(wanted, dependencies, available, builtins):
    # Alpine's installed package may compress modules that the boot initramfs
    # stored uncompressed. Resolve the same module before walking dependencies.
    canonical = {}
    for name in available:
        plain = re.sub(r'\.(?:gz|xz|zst)$', '', name)
        if plain in canonical and canonical[plain] != name:
            raise ValueError('ambiguous kernel module encoding')
        canonical[plain] = name
    selected = set()
    pending = [canonical.get(re.sub(r'\.(?:gz|xz|zst)$', '', name), name) for name in wanted]
    while pending:
        name = pending.pop()
        if name in selected or name in builtins:
            continue
        if name not in available or name not in dependencies:
            raise ValueError('required kernel module unavailable: ' + name)
        selected.add(name)
        pending.extend(dependencies[name])
    return selected


def requested_modules(original, profile):
    if profile is None or 'required_modules' not in profile:
        wanted = {name.split('/', 3)[3] for name, _, _, _ in original
                  if name.startswith('lib/modules/') and '.ko' in name}
        return wanted | {'kernel/fs/ext4/ext4.ko'}
    wanted = profile['required_modules']
    if (not isinstance(wanted, list) or not 1 <= len(wanted) <= 64
            or any(not isinstance(name, str) or not re.fullmatch(r'kernel/[A-Za-z0-9_./+-]+\.ko', name)
                   or '..' in PurePosixPath(name).parts for name in wanted)
            or not VZ_BOOT_MODULES.issubset(wanted)):
        raise ValueError('explicit module profile must include the bounded VZ/ext4 boot drivers')
    return set(wanted)


def verify_package(package, trusted_key):
    """Authenticate APKv2 control metadata and its complete compressed payload."""
    for path, limit in ((package, 128 * 1024**2), (trusted_key, 8192)):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError('bounded regular package and public key required')
    if hashlib.sha256(trusted_key.read_bytes()).hexdigest() != ALPINE_KEY_SHA256:
        raise ValueError('public key differs from existing trusted anchor')
    raw = package.read_bytes()
    signature_tar, signature_size = inflate(raw, 1024**2)
    control, control_size = inflate(raw[signature_size:], 1024**2)
    signed = raw[signature_size:signature_size + control_size]
    with tarfile.open(fileobj=io.BytesIO(signature_tar), mode='r:') as archive:
        members = archive.getmembers()
        if (len(members) != 1 or members[0].name != '.SIGN.RSA.' + ALPINE_KEY_NAME
                or not members[0].isfile() or not 1 <= members[0].size <= 1024):
            raise ValueError('unexpected APK signature member')
        signature = archive.extractfile(members[0]).read()
    with tempfile.TemporaryDirectory(prefix='vos-alpine-signature-', dir='/private/tmp') as name:
        signature_path = Path(name) / 'signature'
        signature_path.write_bytes(signature)
        checked = subprocess.run(['openssl', 'dgst', '-sha1', '-verify', str(trusted_key),
            '-signature', str(signature_path)], input=signed, capture_output=True, timeout=10)
        if checked.returncode:
            raise ValueError('APK signature verification failed')
    with tarfile.open(fileobj=io.BytesIO(control), mode='r:') as archive:
        member = archive.getmember('.PKGINFO')
        if not member.isfile() or member.size > 128 * 1024:
            raise ValueError('invalid authenticated package metadata')
        pairs = [line.split(' = ', 1) for line in archive.extractfile(member).read().decode().splitlines()
                 if ' = ' in line]
    for key in ('pkgname', 'pkgver', 'arch', 'datahash'):
        if sum(name == key for name, value in pairs) != 1:
            raise ValueError('ambiguous authenticated package identity')
    meta = dict(pairs)
    if not re.fullmatch(r'[0-9a-f]{64}', meta['datahash']):
        raise ValueError('authenticated package data hash required')
    if hashlib.sha256(raw[signature_size + control_size:]).hexdigest() != meta['datahash']:
        raise ValueError('package data differs from authenticated hash')
    return dict(schema='virtualos-linux-package-audit/v1', package=meta['pkgname'],
        version=meta['pkgver'], arch=meta['arch'], package_sha256=hashlib.sha256(raw).hexdigest(),
        data_sha256=meta['datahash'], signature_key=ALPINE_KEY_NAME,
        trusted_key_sha256=ALPINE_KEY_SHA256, signature_verified=True)


def prepare(package, initrd, output, *, profile=None, trusted_key=None):
    package_sha256, package_version, release = PACKAGE_SHA256, '6.6.142-r0', RELEASE
    if profile is not None:
        if trusted_key is None:
            raise ValueError('explicit profile requires the existing trusted public key')
        authenticated = verify_package(package, trusted_key)
        if (any(profile.get(key) != value for key, value in authenticated.items())
                or authenticated['package'] != 'linux-virt' or authenticated['arch'] != 'aarch64'
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-r[0-9]+', authenticated['version'])):
            raise ValueError('explicit profile differs from authenticated kernel identity')
        package_sha256, package_version = authenticated['package_sha256'], authenticated['version']
        release = package_version.replace('-r', '-') + '-virt'
    for path in (package, initrd):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 128 * 1024**2:
            raise ValueError('bounded regular boot artifacts required')
    raw = package.read_bytes()
    if hashlib.sha256(raw).hexdigest() != package_sha256:
        raise ValueError('package differs from signature-verified pin')
    _, signature_size = inflate(raw, 1024**2)
    control, control_size = inflate(raw[signature_size:], 1024**2)
    data = raw[signature_size + control_size:]
    with tarfile.open(fileobj=io.BytesIO(control), mode='r:') as archive:
        meta = dict(line.split(' = ', 1) for line in archive.extractfile('.PKGINFO').read().decode().splitlines()
                    if ' = ' in line)
    if (meta['pkgname'], meta['pkgver'], meta['arch']) != ('linux-virt', package_version, 'aarch64'):
        raise ValueError('unexpected signed package identity')
    if hashlib.sha256(data).hexdigest() != meta['datahash']:
        raise ValueError('package data differs from signed hash')
    old, consumed = inflate(initrd.read_bytes(), 128 * 1024**2)
    if consumed != initrd.stat().st_size:
        raise ValueError('unexpected initramfs gzip members')
    original = list(entries(old))
    wanted = requested_modules(original, profile)
    prefix = 'lib/modules/' + release + '/'
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = {member.name: member for member in archive.getmembers()}

        def read(name):
            member = members[name]
            if not member.isfile() or not 0 <= member.size <= 64 * 1024**2:
                raise ValueError('invalid package artifact')
            return archive.extractfile(member).read()

        dependencies = {}
        for line in read(prefix + 'modules.dep').decode().splitlines():
            module, separator, deps = line.partition(':')
            if separator:
                dependencies[module] = deps.split()
        builtins = set(read(prefix + 'modules.builtin').decode().splitlines())
        selected = dependency_closure(wanted, dependencies,
            {name[len(prefix):] for name in members if name.startswith(prefix)}, builtins)
        additions = {prefix + name: read(prefix + name) for name in sorted(selected)}
        for name in members:
            if name.startswith(prefix) and '/' not in name[len(prefix):] and members[name].isfile():
                additions[name] = read(name)
        vmlinuz = read('boot/vmlinuz-virt')
    image = None
    offset = 0
    while (offset := vmlinuz.find(b'\x1f\x8b\x08', offset)) >= 0:
        try:
            candidate, _ = inflate(vmlinuz[offset:], 64 * 1024**2)
            if ('Linux version ' + release + ' ').encode() in candidate:
                image = candidate
                break
        except (ValueError, zlib.error):
            pass
        offset += 1
    if image is None:
        raise ValueError('matching uncompressed kernel Image unavailable')
    parents = set()
    for name in additions:
        if '..' in PurePosixPath(name).parts:
            raise ValueError('unsafe module path')
        parents.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != '.')
    preserved = [(name, raw) for name, _, _, raw in original if not name.startswith('lib/modules/')]
    existing = {name for name, _ in preserved}
    chunks = [raw for _, raw in preserved]
    inode = 1000000
    for name in sorted(parents - existing, key=lambda p: (p.count('/'), p)):
        chunks.append(record(name, b'', inode, 0o40755)); inode += 1
    for name, body in sorted(additions.items()):
        chunks.append(record(name, body, inode)); inode += 1
    chunks.append(record('TRAILER!!!', b'', inode))
    packed = b''.join(chunks)
    packed += b'\0' * (-len(packed) % 512)
    encoded = gzip.compress(packed, compresslevel=1, mtime=0)
    output.mkdir(mode=0o700)  # Never overwrite a prior build or active artifact.
    (output / 'Image').write_bytes(image)
    (output / 'initramfs').write_bytes(encoded)
    checked = audit(output / 'Image', output / 'initramfs')
    if checked['linux_artifacts'] != 'ready':
        raise ValueError('prepared boot artifact admission failed')
    receipt = dict(schema='virtualos-linux-ext4-artifacts/v1', package_sha256=package_sha256,
        source_initramfs_sha256=hashlib.sha256(initrd.read_bytes()).hexdigest(),
        kernel_sha256=hashlib.sha256(image).hexdigest(), initramfs_sha256=hashlib.sha256(encoded).hexdigest(),
        kernel_bytes=len(image), initramfs_bytes=len(encoded), modules=len(selected),
        artifact_check=checked, boot_verified=False, persistent_linux_verified=False)
    if profile is not None:
        receipt['authenticated_package'] = authenticated
        receipt['requested_modules'] = sorted(wanted)
    (output / 'ready.json').write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
    return receipt


if __name__ == '__main__':
    os.umask(0o077)
    try:
        if len(sys.argv) not in (4, 6):
            raise ValueError('package, initramfs, output and optional profile/public-key required')
        options = {} if len(sys.argv) == 4 else dict(profile=json.loads(Path(sys.argv[4]).read_text()),
                                                   trusted_key=Path(sys.argv[5]))
        print(json.dumps(prepare(*(Path(arg) for arg in sys.argv[1:4]), **options), sort_keys=True))
    except (OSError, ValueError, KeyError, tarfile.TarError, zlib.error) as error:
        raise SystemExit('linux_prepare=refused: ' + type(error).__name__)
