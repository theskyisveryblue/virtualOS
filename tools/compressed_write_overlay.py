"""Checksummed compressed writes, retaining the existing logical overlay limit.

The blocks view exposes raw bytes to the established transaction and NBD code.
A different base identity prevents older readers from opening this format.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import zlib

from cloud_disk import BLOCK, RawDisk, digest
from compressed_delta import CompressedDeltaDisk, base_identity, file_identity
from overlay_compact import no_open_files, regular, identity

FORMAT = 'virtualos-compressed-writes/v1'
SCHEMA = {
    'encoded_blocks': 'CREATE TABLE encoded_blocks (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)',
    'blocks': 'CREATE VIEW blocks AS SELECT idx, vos_overlay_decode_v1(data) AS data FROM encoded_blocks',
    'blocks_insert': ('CREATE TRIGGER blocks_insert INSTEAD OF INSERT ON blocks BEGIN '
                      'INSERT OR REPLACE INTO encoded_blocks(idx,data) VALUES '
                      '(NEW.idx,vos_overlay_encode_v1(NEW.data)); END'),
    'blocks_delete': ('CREATE TRIGGER blocks_delete INSTEAD OF DELETE ON blocks BEGIN '
                      'DELETE FROM encoded_blocks WHERE idx=OLD.idx; END'),
}


def encode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= BLOCK:
        raise ValueError('bounded raw overlay block required')
    compressed = zlib.compress(raw, 1)
    mode, payload = (b'Z', compressed) if len(compressed) < len(raw) else (b'R', raw)
    return struct.pack('>I', len(raw)) + hashlib.sha256(raw).digest() + mode + payload


def decode(encoded):
    if not isinstance(encoded, bytes) or not 37 < len(encoded) <= BLOCK + 37:
        raise ValueError('invalid encoded overlay block')
    size = struct.unpack('>I', encoded[:4])[0]
    if not 0 < size <= BLOCK:
        raise ValueError('invalid decoded overlay size')
    if encoded[36:37] == b'R':
        raw = encoded[37:]
    elif encoded[36:37] == b'Z':
        decoder = zlib.decompressobj()
        try:
            raw = decoder.decompress(encoded[37:], size + 1)
        except zlib.error:
            raise ValueError('corrupt compressed overlay') from None
        if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise ValueError('invalid compressed overlay stream')
    else:
        raise ValueError('unknown overlay encoding')
    if len(raw) != size or hashlib.sha256(raw).digest() != encoded[4:36]:
        raise ValueError('overlay block integrity failure')
    return raw


def layered_identity(base_pin, delta_pin, compressed):
    prefix = b'virtualos-compressed-writes/v1\0' if compressed else b'virtualos-layered-base/v1\0'
    return digest(prefix + bytes.fromhex(base_pin) + bytes.fromhex(delta_pin))


def check_schema(db, compressed):
    objects = {name: (kind, sql) for kind, name, sql in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    expected = {'metadata', 'blocks', 'encoded_blocks', 'blocks_insert', 'blocks_delete'} if compressed else {'metadata', 'blocks'}
    if set(objects) != expected or objects['metadata'][0] != 'table':
        raise ValueError('unexpected overlay schema')
    if [r[1] for r in db.execute('PRAGMA table_info(metadata)')] != ['name', 'value']:
        raise ValueError('unexpected overlay metadata columns')
    if compressed:
        for name, sql in SCHEMA.items():
            if objects[name][1] != sql:
                raise ValueError('compressed overlay schema changed')
    elif objects['blocks'][0] != 'table' or [r[1] for r in db.execute('PRAGMA table_info(blocks)')] != ['idx', 'data']:
        raise ValueError('unexpected raw overlay columns')


class CompressedWriteDisk(CompressedDeltaDisk):
    def _open_overlay(self, overlay, identity_hash):
        if (self.delta_metadata.get('base_identity') != identity_hash or
                self.delta_metadata.get('logical_size') != str(self.size)):
            raise ValueError('compressed delta belongs to a different base')
        RawDisk._open_overlay(self, overlay, layered_identity(identity_hash, self.delta_sha256, True))
        try:
            self.db.create_function('vos_overlay_encode_v1', 1, encode, deterministic=True)
            self.db.create_function('vos_overlay_decode_v1', 1, decode, deterministic=True)
            codec = self.db.execute("SELECT value FROM metadata WHERE name='overlay_codec'").fetchone()
            if codec is None:
                check_schema(self.db, False)
                if self.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]:
                    raise ValueError('new compressed format requires an empty overlay')
                with self.db:
                    self.db.execute('DROP TABLE blocks')
                    for sql in SCHEMA.values():
                        self.db.execute(sql)
                    self.db.execute('INSERT INTO metadata VALUES (?,?)', ('overlay_codec', FORMAT))
            elif codec != (FORMAT,):
                raise ValueError('unsupported write overlay codec')
            check_schema(self.db, True)
        except BaseException:
            self.db.close()
            raise


def logical_fingerprint(db, size):
    checksum = hashlib.sha256(b'virtualos-overlay-blocks/v1\0')
    count, previous = 0, -1
    for index, raw in db.execute('SELECT idx,data FROM blocks ORDER BY idx'):
        if (type(index) is not int or not previous < index < (size + BLOCK - 1) // BLOCK
                or not isinstance(raw, bytes) or len(raw) != min(BLOCK, size - index * BLOCK)):
            raise ValueError('invalid logical overlay block')
        checksum.update(struct.pack('>QQ', index, len(raw)))
        checksum.update(raw)
        count += 1
        previous = index
    return checksum.hexdigest(), count


def convert_copy(base, delta, delta_pin, source, destination, *, to_compressed=True,
                 reserve=10 * 1024**3, overlay_limit_bytes=2 * 1024**3, probe=no_open_files):
    """Convert a closed overlay to a new file, retaining its source unchanged."""
    base, delta, source, destination = map(Path, (base, delta, source, destination))
    for path in (base, delta, source, destination):
        if not path.is_absolute() or '..' in path.parts or path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError('literal unlinked absolute paths required')
        if (Path.home() / 'VirtualOSMachines').resolve() in path.parents:
            raise ValueError('production store refused')
    if type(to_compressed) is not bool or type(reserve) is not int or reserve < 0:
        raise ValueError('invalid conversion policy')
    if type(overlay_limit_bytes) is not int or not BLOCK <= overlay_limit_bytes <= 2 * 1024**3:
        raise ValueError('logical write limit may not exceed 2 GiB')
    before = regular(source)
    backing_before = [file_identity(path.stat()) for path in (base, delta)]
    if before.st_size > 4 * 1024**3:
        raise ValueError('oversized source overlay')
    for parent in (source.parent, destination.parent):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private owned overlay parents required')
    if destination.exists():
        raise ValueError('new destination required')
    fd = os.open(source.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    temporary = None
    with os.fdopen(fd, 'r+b') as lock:
        regular(source.with_suffix('.lock'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in (source, *(Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
            if path.exists() or path.is_symlink():
                info = regular(path)
                if str(path).endswith(('-wal', '-journal')) and info.st_size:
                    raise ValueError('source must be closed without pending journals')
                if not probe(path):
                    raise ValueError('source open-file check failed')
        if identity(regular(source)) != identity(before):
            raise ValueError('source changed during admission')
        free = os.statvfs(destination.parent)
        if free.f_bavail * free.f_frsize < reserve + 2 * before.st_size + 4 * 1024**2:
            raise OSError('insufficient conversion headroom')
        descriptor, name = tempfile.mkstemp(prefix='.write-overlay-', dir=destination.parent)
        os.close(descriptor)
        temporary = Path(name)
        reader, writer = None, None
        try:
            reader = sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)
            reader.create_function('vos_overlay_decode_v1', 1, decode, deterministic=True)
            compressed_source = reader.execute("SELECT value FROM metadata WHERE name='overlay_codec'").fetchone()
            if compressed_source not in (None, (FORMAT,)):
                raise ValueError('unknown source overlay codec')
            check_schema(reader, bool(compressed_source))
            if reader.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('source overlay integrity check failed')
            base_pin = base_identity(base, base.stat())
            expected = layered_identity(base_pin, delta_pin, bool(compressed_source))
            if reader.execute("SELECT value FROM metadata WHERE name='manifest_sha256'").fetchone() != (expected,):
                raise ValueError('source overlay belongs to another disk')
            expected_fingerprint, count = logical_fingerprint(reader, base.stat().st_size)
            if count * BLOCK > overlay_limit_bytes:
                raise OSError('source exceeds logical write limit')
            klass = CompressedWriteDisk if to_compressed else CompressedDeltaDisk
            writer = klass(base, delta, temporary, delta_pin,
                           overlay_limit_bytes=overlay_limit_bytes, min_host_free_bytes=reserve)
            with writer.db:
                for ordinal, (index, raw) in enumerate(reader.execute('SELECT idx,data FROM blocks ORDER BY idx')):
                    if ordinal % 128 == 0:
                        writer._ensure_host_space(16 * 1024**2)
                    writer.db.execute('INSERT INTO blocks(idx,data) VALUES (?,?)', (index, raw))
            if logical_fingerprint(writer.db, writer.size) != (expected_fingerprint, count):
                raise ValueError('converted overlay changed logical contents')
            writer.close()
            writer = None
            reader.close()
            reader = None
            if identity(regular(source)) != identity(before):
                raise ValueError('source changed during conversion')
            if [file_identity(path.stat()) for path in (base, delta)] != backing_before:
                raise ValueError('immutable backing changed during conversion')
            with sqlite3.connect(temporary) as sealed:
                if sealed.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
                    raise OSError('conversion journal did not seal')
            if Path(str(temporary) + '-wal').exists():
                raise OSError('conversion retained a pending WAL')
            Path(str(temporary) + '-shm').unlink(missing_ok=True)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.link(temporary, destination)
            temporary.unlink()
            temporary = None
            fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return dict(source_bytes=before.st_size, destination_bytes=destination.stat().st_size,
                        blocks=count, logical_fingerprint=expected_fingerprint,
                        output_format=FORMAT if to_compressed else 'raw', source_unchanged=True)
        finally:
            try:
                if writer is not None:
                    writer.close()
            finally:
                try:
                    if reader is not None:
                        reader.close()
                finally:
                    if temporary is not None:
                        for path in (temporary, *(Path(str(temporary) + suffix) for suffix in ('-wal', '-shm'))):
                            path.unlink(missing_ok=True)
