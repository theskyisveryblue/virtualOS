"""Immutable compressed machine changes below a separately bounded write overlay."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import zlib

from cloud_disk import BLOCK, RawDisk, digest

FORMAT = 'virtualos-compressed-delta/v1'


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def base_identity(path, info):
    return digest(json.dumps([str(path.resolve()), *file_identity(info)]).encode())


def file_hash(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for piece in iter(lambda: stream.read(1024**2), b''):
            checksum.update(piece)
    return checksum.hexdigest()


def build(base, machine, destination, *, reserve=10 * 1024**3, stored_limit=2 * 1024**3):
    """Build from frozen scratch copies; publish only a complete new artifact."""
    base, machine, destination = map(Path, (base, machine, destination))
    for path in (base, machine, destination):
        if path.is_symlink() or (Path.home()/'VirtualOSMachines').resolve() in path.resolve().parents:
            raise ValueError('production store and linked paths refused')
    parent = destination.parent.lstat()
    if (destination.exists() or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError('new destination in private owned directory required')
    if (type(reserve) is not int or reserve < 0 or type(stored_limit) is not int
            or not BLOCK <= stored_limit <= 2*1024**3):
        raise ValueError('invalid storage budget')
    descriptors = []
    temporary = None
    try:
        for path in (base, machine):
            descriptors.append(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK))
        before = [os.fstat(fd) for fd in descriptors]
        if (any(not stat.S_ISREG(s.st_mode) or not 0 < s.st_size <= 64*1024**3 or s.st_size % 512 for s in before)
                or before[0].st_size != before[1].st_size):
            raise ValueError('equal aligned regular disk copies required')
        fd, name = tempfile.mkstemp(prefix='.compressed-delta-', dir=destination.parent)
        os.close(fd); temporary = Path(name)
        database = sqlite3.connect(temporary)
        hashes = [hashlib.sha256(), hashlib.sha256()]
        count = raw_bytes = encoded_bytes = 0
        try:
            database.execute('CREATE TABLE blocks (idx INTEGER PRIMARY KEY, raw_size INTEGER NOT NULL, sha256 TEXT NOT NULL, data BLOB NOT NULL)')
            database.execute('CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            with database:
                for offset in range(0, before[0].st_size, BLOCK):
                    if offset % (16*1024**2) == 0:
                        free = os.statvfs(destination.parent)
                        if free.f_bavail*free.f_frsize < reserve + 32*1024**2:
                            raise OSError('compressed delta must preserve host reserve')
                    length = min(BLOCK, before[0].st_size-offset)
                    first, second = [os.pread(fd, length, offset) for fd in descriptors]
                    if len(first) != length or len(second) != length:
                        raise ValueError('disk copy truncated')
                    hashes[0].update(first); hashes[1].update(second)
                    if first == second:
                        continue
                    encoded = zlib.compress(second, 1)
                    encoded_bytes += len(encoded)
                    if encoded_bytes + (count+1)*256 + 64*1024 > stored_limit:
                        raise OSError('compressed delta storage limit reached')
                    database.execute('INSERT INTO blocks VALUES (?,?,?,?)',
                                     (offset//BLOCK, length, digest(second), encoded))
                    count += 1; raw_bytes += length
                metadata = dict(format=FORMAT, logical_size=str(before[0].st_size),
                                base_identity=base_identity(base,before[0]),
                                base_sha256=hashes[0].hexdigest(), machine_sha256=hashes[1].hexdigest(),
                                block_count=str(count), raw_delta_bytes=str(raw_bytes))
                database.executemany('INSERT INTO metadata VALUES (?,?)', metadata.items())
        finally:
            database.close()
        if any(file_identity(os.fstat(fd)) != file_identity(info) for fd,info in zip(descriptors,before)):
            raise ValueError('disk copy changed during delta build')
        if temporary.stat().st_size > stored_limit:
            raise OSError('compressed delta database exceeds storage budget')
        with temporary.open('rb') as stream: os.fsync(stream.fileno())
        checksum = file_hash(temporary)
        temporary.chmod(0o400)
        os.link(temporary,destination); temporary.unlink(); temporary=None
        directory = os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
        return dict(format=FORMAT,delta_sha256=checksum,blocks=count,raw_delta_bytes=raw_bytes,
                    encoded_payload_bytes=encoded_bytes,stored_bytes=destination.stat().st_size,
                    base_sha256=metadata['base_sha256'],machine_sha256=metadata['machine_sha256'])
    finally:
        for descriptor in descriptors: os.close(descriptor)
        if temporary is not None: temporary.unlink(missing_ok=True)


class CompressedDeltaDisk(RawDisk):
    def __init__(self, source, delta, overlay, delta_sha256, **budgets):
        self.delta_path = Path(delta)
        self.delta_fd = os.open(self.delta_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        self.delta_db = None
        try:
            info=os.fstat(self.delta_fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 2*1024**3:
                raise ValueError('bounded regular compressed delta required')
            self.delta_identity=file_identity(info)
            if not isinstance(delta_sha256,str) or len(delta_sha256)!=64 or file_hash(self.delta_path)!=delta_sha256:
                raise ValueError('compressed delta SHA-256 mismatch')
            self.delta_sha256=delta_sha256
            self.delta_db=sqlite3.connect(self.delta_path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
            self.delta_metadata=dict(self.delta_db.execute('SELECT name,value FROM metadata'))
            if self.delta_metadata.get('format')!=FORMAT:
                raise ValueError('unsupported compressed delta format')
            self._check_delta()
            super().__init__(source,overlay,**budgets)
        except BaseException:
            if self.delta_db is not None:self.delta_db.close()
            os.close(self.delta_fd)
            raise

    def _check_delta(self):
        if (file_identity(os.fstat(self.delta_fd))!=self.delta_identity
                or file_identity(self.delta_path.stat())!=self.delta_identity):
            raise OSError('immutable compressed delta changed')

    def _open_overlay(self, overlay, identity_hash):
        if (self.delta_metadata.get('base_identity')!=identity_hash
                or self.delta_metadata.get('logical_size')!=str(self.size)):
            raise ValueError('compressed delta belongs to a different base')
        compound=digest(b'virtualos-layered-base/v1\0'+bytes.fromhex(identity_hash)+bytes.fromhex(self.delta_sha256))
        # Plain/legacy RawDisk sees a different identity and must refuse this
        # overlay rather than accidentally omitting its initial delta layer.
        super()._open_overlay(overlay,compound)

    def _base_block(self,index):
        self._check_delta()
        row=self.delta_db.execute('SELECT raw_size,sha256,data FROM blocks WHERE idx=?',(index,)).fetchone()
        if row is None:return super()._base_block(index)
        expected=min(BLOCK,self.size-index*BLOCK)
        length,pin,encoded=row
        if length!=expected or not isinstance(encoded,bytes) or not 0<len(encoded)<=BLOCK+128:
            raise ValueError('invalid compressed delta block')
        decoder=zlib.decompressobj()
        try:raw=decoder.decompress(encoded,expected+1)
        except zlib.error:raise ValueError('invalid compressed delta payload') from None
        if (len(raw)!=expected or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data
                or digest(raw)!=pin):
            raise ValueError('compressed delta block integrity failure')
        return raw

    def close(self):
        try:super().close()
        finally:
            self.delta_db.close();os.close(self.delta_fd)
