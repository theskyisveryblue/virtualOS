"""Build a separate signed Alpine 3.24 installer; never select a live guest."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import sys
import tarfile

from linux_boot_artifacts import audit
from linux_prepare_ext4 import ALPINE_KEY_SHA256, entries, inflate, record, verify_package


def safe_name(name):
    name=name.removeprefix('./').rstrip('/')
    if not name or name.startswith('/') or '..' in PurePosixPath(name).parts:
        raise ValueError('unsafe maintained installer path')
    return name


def resolve(files,name):
    parts=[]
    pending=name.lstrip('/').split('/')
    links=0
    while pending:
        item=pending.pop(0)
        if item in ('','.'):continue
        if item=='..':
            if not parts:raise ValueError('installer link escapes guest root')
            parts.pop();continue
        parts.append(item)
        current='/'.join(parts)
        if current not in files:raise ValueError('installer dependency missing: '+current)
        mode,body,_=files[current]
        if stat.S_ISLNK(mode):
            links+=1
            if links>32:raise ValueError('installer link loop')
            target=body.decode()
            parts=[] if target.startswith('/') else parts[:-1]
            pending=target.lstrip('/').split('/')+pending
    return files['/'.join(parts)]


def validate_binary(files,name):
    mode,data,_=resolve(files,name)
    if not stat.S_ISREG(mode) or not mode&0o111 or len(data)<64 or data[:6]!=b'\x7fELF\x02\x01':
        raise ValueError('maintained aarch64 ELF executable required: '+name)
    if struct.unpack_from('<H',data,18)[0]!=183:
        raise ValueError('wrong installer executable architecture')
    offset=struct.unpack_from('<Q',data,32)[0]
    size,count=struct.unpack_from('<HH',data,54)
    if size<56 or count>128 or offset+size*count>len(data):raise ValueError('invalid ELF program headers')
    for number in range(count):
        header=offset+size*number
        if struct.unpack_from('<I',data,header)[0]==3:
            start=struct.unpack_from('<Q',data,header+8)[0]
            length=struct.unpack_from('<Q',data,header+32)[0]
            if not 1<=length<=4096 or start+length>len(data):raise ValueError('invalid ELF interpreter')
            interpreter=data[start:start+length].rstrip(b'\0').decode()
            resolved=resolve(files,interpreter)
            if not stat.S_ISREG(resolved[0]):raise ValueError('ELF interpreter is not a regular file')


def package_files(package):
    raw=package.read_bytes()
    _,first=inflate(raw,1048576);_,second=inflate(raw[first:],1048576)
    with tarfile.open(fileobj=io.BytesIO(raw[first+second:]),mode='r:gz') as archive:
        members=archive.getmembers()
        if len(members)>10000:raise ValueError('package member count exceeds bound')
        total=0
        for member in members:
            if member.name in ('','.','./'):continue
            name=safe_name(member.name)
            if member.isdir():yield name,stat.S_IFDIR|member.mode,b''
            elif member.issym():yield name,stat.S_IFLNK|0o777,member.linkname.encode()
            elif member.isfile() or member.islnk():
                if member.islnk():safe_name(member.linkname)
                if not 0<=member.size<=64*1024**2:raise ValueError('package member exceeds bound')
                body=archive.extractfile(member).read(64*1024**2+1)
                if len(body)>64*1024**2:raise ValueError('package hardlink exceeds bound')
                total+=len(body)
                if total>128*1024**2:raise ValueError('package expands beyond bound')
                yield name,stat.S_IFREG|member.mode,body
            else:raise ValueError('unsupported maintained package member')


def pack(files,device_records):
    chunks=[]
    for inode,(name,(mode,body,origin)) in enumerate(sorted(files.items(),key=lambda row:(row[0].count('/'),row[0])),1000000):
        if name in device_records and origin=='kernel-or-device-structure':
            chunks.append(device_records[name])
        else:chunks.append(record(name,body,inode,mode))
    chunks.append(record('TRAILER!!!',b'',2000000))
    packed=b''.join(chunks);packed+=b'\0'*(-len(packed)%512)
    return gzip.compress(packed,compresslevel=1,mtime=0)


def build(stage,output):
    stage=stage.resolve()
    profile=json.loads((stage/'rootfs-profile.json').read_text())
    if (profile.get('branch')!='v3.24' or profile.get('arch')!='aarch64'
            or profile.get('repository')!='https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/'
            or profile.get('trusted_key_sha256')!=ALPINE_KEY_SHA256):
        raise ValueError('explicit authenticated Alpine 3.24 main profile required')
    key=stage/'alpine-devel-616ae350.rsa.pub'
    boot=stage/'prepared-3.24'
    boot_receipt=json.loads((boot/'ready.json').read_text())
    if 'kernel/net/packet/af_packet.ko' not in boot_receipt.get('requested_modules',[]):
        raise ValueError('maintained DHCP requires authenticated packet socket driver')
    image=(boot/'Image').read_bytes();encoded=(boot/'initramfs').read_bytes()
    if (hashlib.sha256(image).hexdigest()!=boot_receipt['kernel_sha256']
            or hashlib.sha256(encoded).hexdigest()!=boot_receipt['initramfs_sha256']):
        raise ValueError('prepared maintained boot pair changed')
    unpacked,consumed=inflate(encoded,128*1024**2)
    if consumed!=len(encoded):raise ValueError('unexpected bootstrap archive member')
    files={}
    device_records={}
    for name,mode,body,raw_record in entries(unpacked):
        if name=='.':continue
        # Preserve only structure, device nodes and already-authenticated 6.18
        # modules. No regular file from the old 3.20 userland is carried over.
        if stat.S_ISDIR(mode) or (name.startswith('dev/') and stat.S_ISCHR(mode)) or name.startswith('lib/modules/'):
            files[name]=(mode,body,'kernel-or-device-structure')
            if stat.S_ISCHR(mode):device_records[name]=raw_record
    package_rows=profile.get('packages')
    if not isinstance(package_rows,list) or not 1<=len(package_rows)<=64:raise ValueError('bounded package closure required')
    checksums=[]
    identities={}
    for row in package_rows:
        filename=safe_name(row['filename'])
        if '/' in filename:raise ValueError('package cache filename must be flat')
        path=stage/'rootfs-packages'/filename
        verified=verify_package(path,key)
        if any(row.get(field)!=verified[field] for field in ('package','version','arch','package_sha256','data_sha256')):
            raise ValueError('staged package differs from signed explicit profile')
        identities[verified['package']]=verified
        for name,mode,body in package_files(path):
            files[name]=(mode,body,verified['package_sha256'])
        cache_name='var/cache/apk/packages/'+filename
        files[cache_name]=(stat.S_IFREG|0o600,path.read_bytes(),verified['package_sha256'])
        checksums.append(verified['package_sha256']+'  '+filename)
    for name in ('alpine-base','alpine-release'):
        if not identities[name]['version'].startswith('3.24.'):raise ValueError('unsupported rootfs identity')
    if not identities['apk-tools']['version'].startswith('3.'):raise ValueError('maintained apk-tools required')
    applets=files['etc/busybox-paths.d/busybox'][1].decode().splitlines()
    for name in applets:
        name=safe_name(name)
        files.setdefault(name,(stat.S_IFLNK|0o777,b'/bin/busybox','signed-busybox-applets'))
    files['init']=(stat.S_IFREG|0o755,files['usr/share/mkinitfs/initramfs-init'][1],
                   identities['mkinitfs']['package_sha256'])
    files['etc/passwd']=(stat.S_IFREG|0o644,b'root:x:0:0:root:/root:/bin/sh\n','locked-root-policy')
    files['etc/shadow']=(stat.S_IFREG|0o600,b'root:!:0:0:99999:7:::\n','locked-root-policy')
    files['etc/apk/repositories']=(stat.S_IFREG|0o644,b'https://dl-cdn.alpinelinux.org/alpine/v3.24/main\n','maintained-main-policy')
    files['var/cache/apk/packages/SHA256SUMS']=(stat.S_IFREG|0o600,('\n'.join(checksums)+'\n').encode(),'authenticated-package-manifest')
    embedded=dict(schema='virtualos-maintained-linux-installer/v1',branch='v3.24',
        packages=len(package_rows),all_package_signatures_verified=True,root_login_locked=True,
        bootstrap_userland='signature-verified Alpine 3.24 packages',
        rootfs_profile_sha256=hashlib.sha256((stage/'rootfs-profile.json').read_bytes()).hexdigest())
    files['etc/virtualos-lab-profile.json']=(stat.S_IFREG|0o600,(json.dumps(embedded,sort_keys=True)+'\n').encode(),'maintained-profile')
    for name in list(files):
        for parent in PurePosixPath(name).parents:
            if str(parent)!='.':files.setdefault(str(parent),(stat.S_IFDIR|0o755,b'','directory-structure'))
    for name in ('bin/busybox','sbin/apk','sbin/mkfs.ext4','sbin/nlplug-findfs','sbin/modprobe'):
        validate_binary(files,name)
    if b'emergency recovery shell' not in files['init'][1]:raise ValueError('supported installer shell marker missing')
    if sum(len(row[1]) for row in files.values())>128*1024**2:raise ValueError('maintained installer exceeds bound')
    maintained=pack(files,device_records)
    output.mkdir(mode=0o700)
    (output/'Image').write_bytes(image);(output/'initramfs').write_bytes(maintained)
    checked=audit(output/'Image',output/'initramfs')
    if checked['linux_artifacts']!='ready':raise ValueError('maintained module audit failed')
    receipt=dict(schema='virtualos-maintained-linux-profile/v1',branch='v3.24',
        kernel_sha256=hashlib.sha256(image).hexdigest(),initramfs_sha256=hashlib.sha256(maintained).hexdigest(),
        kernel_bytes=len(image),initramfs_bytes=len(maintained),artifact_check=checked,
        rootfs_profile_sha256=embedded['rootfs_profile_sha256'],all_package_signatures_verified=True,
        rootfs_packages=len(package_rows),bootstrap_userland=embedded['bootstrap_userland'],
        legacy_regular_userland_files_retained=0,root_login_locked=True,
        boot_verified=False,persistent_linux_verified=False)
    (output/'profile.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
    return receipt


def check(profile_path):
    info=profile_path.lstat();parent=profile_path.parent.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077
            or info.st_nlink!=1 or not 0<info.st_size<=65536
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.getuid() or parent.st_mode&0o077):
        raise ValueError('private owned maintained profile required')
    profile=json.loads(profile_path.read_text())
    if (profile.get('schema')!='virtualos-maintained-linux-profile/v1' or profile.get('branch')!='v3.24'
            or profile.get('all_package_signatures_verified') is not True
            or type(profile.get('legacy_regular_userland_files_retained')) is not int
            or profile['legacy_regular_userland_files_retained']!=0 or profile.get('root_login_locked') is not True):
        raise ValueError('maintained installer profile required')
    for name,field in (('Image','kernel_sha256'),('initramfs','initramfs_sha256')):
        path=profile_path.parent/name
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=128*1024**2:
            raise ValueError('bounded regular maintained artifact required')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=profile[field]:raise ValueError('maintained artifact pin mismatch')
    checked=audit(profile_path.parent/'Image',profile_path.parent/'initramfs')
    if checked['linux_artifacts']!='ready':raise ValueError('maintained boot artifact audit failed')
    return profile


if __name__=='__main__':
    os.umask(0o077)
    if len(sys.argv)==4 and sys.argv[1]=='build':
        print(json.dumps(build(Path(sys.argv[2]),Path(sys.argv[3])),sort_keys=True))
    elif len(sys.argv)==3 and sys.argv[1]=='check':
        print(json.dumps(check(Path(sys.argv[2])),sort_keys=True))
    else:raise SystemExit('maintained profile build STAGE OUTPUT | check PROFILE')
