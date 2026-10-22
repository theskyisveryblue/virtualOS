"""Count redundant rows in a closed raw snapshot without changing disk data."""
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import struct

from cloud_disk import BLOCK, digest
from compressed_delta import CompressedDeltaDisk, file_identity
from overlay_compact import identity, no_open_files, regular


class _ReadOnlyLayers(CompressedDeltaDisk):
    def _open_overlay(self, overlay, identity_hash):
        if (self.delta_metadata.get('base_identity') != identity_hash
                or self.delta_metadata.get('logical_size') != str(self.size)):
            raise ValueError('compressed delta belongs to a different base')
        compound = digest(b'virtualos-layered-base/v1\0' + bytes.fromhex(identity_hash)
                          + bytes.fromhex(self.delta_sha256))
        database = sqlite3.connect(Path(overlay).as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            database.execute('PRAGMA query_only=ON')
            objects = set(database.execute(
                "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            if objects != {('table', 'blocks'), ('table', 'metadata')}:
                raise ValueError('raw snapshot schema required')
            if dict(database.execute('SELECT name,value FROM metadata')) != {'manifest_sha256': compound}:
                raise ValueError('snapshot belongs to a different immutable backing')
        except BaseException:
            database.close()
            raise
        self.db = database

    def close(self):
        # No checkpoint, flush, journal change or schema creation on audited data.
        self.db.close()
        self.delta_db.close()
        os.close(self.delta_fd)
        os.close(self.fd)


def audit(base, seed, seed_sha256, snapshot, *, probe=no_open_files, guard=lambda: None):
    """Audit only a closed snapshot; returned byte estimates are not reclaimed space.

    guard() may abort before admission and every 64 blocks. The caller controls
    host pressure checks and pacing. Memory holds at most one changed block and
    one decoded backing block. Immutable seed hash validation uses a 1 MiB buffer.
    """
    base, seed, snapshot = map(Path, (base, seed, snapshot))
    for path in (base, seed, snapshot):
        if not path.is_absolute() or '..' in path.parts or path.is_symlink():
            raise ValueError('literal absolute unlinked paths required')
        if any(parent.is_symlink() for parent in path.parents):
            raise ValueError('linked parent refused')
        if (Path.home() / 'VirtualOSMachines') in path.parents:
            raise ValueError('production store refused')
    if len({(p.stat().st_dev, p.stat().st_ino) for p in (base, seed, snapshot)}) != 3:
        raise ValueError('three distinct backing and snapshot files required')
    before = regular(snapshot)
    parent = snapshot.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError('private owned snapshot directory required')
    if not 0 < before.st_size <= 4 * 1024**3:
        raise ValueError('bounded snapshot required')
    guard()
    lock_fd = os.open(snapshot.with_suffix('.lock'),
                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(lock_fd, 'r+b') as lock:
        regular(snapshot.with_suffix('.lock'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in (snapshot, *(Path(str(snapshot) + tail) for tail in ('-wal', '-shm', '-journal'))):
            if path.exists() or path.is_symlink():
                info = regular(path)
                if str(path).endswith(('-wal', '-journal')) and info.st_size:
                    raise ValueError('closed snapshot without pending journals required')
                if not probe(path):
                    raise ValueError('snapshot must be closed')
        if identity(regular(snapshot)) != identity(before):
            raise ValueError('snapshot changed during admission')
        disk = _ReadOnlyLayers(base, seed, snapshot, seed_sha256)
        try:
            checksum = hashlib.sha256(b'virtualos-pruning-audit/v1\0')
            rows = redundant = redundant_bytes = raw_bytes = 0
            for index, data in disk.db.execute('SELECT idx,data FROM blocks ORDER BY idx'):
                if rows % 64 == 0:
                    guard()
                if (type(index) is not int or not 0 <= index * BLOCK < disk.size
                        or not isinstance(data, bytes)
                        or len(data) != min(BLOCK, disk.size - index * BLOCK)):
                    raise ValueError('invalid snapshot block')
                checksum.update(struct.pack('>QQ', index, len(data)))
                checksum.update(data)
                if data == disk._base_block(index):
                    redundant += 1
                    redundant_bytes += len(data)
                rows += 1
                raw_bytes += len(data)
            guard()
            disk._check_delta()
            if (file_identity(os.fstat(disk.fd)) != disk.source_identity
                    or file_identity(base.stat()) != disk.source_identity):
                raise ValueError('immutable base changed during audit')
            if identity(regular(snapshot)) != identity(before):
                raise ValueError('snapshot changed during audit')
            return dict(read_only=True, closed_snapshot=True, blocks=rows,
                        raw_block_bytes=raw_bytes, redundant_blocks=redundant,
                        redundant_payload_bytes=redundant_bytes,
                        reusable_page_bytes=disk.db.execute('PRAGMA freelist_count').fetchone()[0]
                        * disk.db.execute('PRAGMA page_size').fetchone()[0],
                        snapshot_bytes=before.st_size, logical_rows_sha256=checksum.hexdigest(),
                        actual_reclaimed_bytes=0,
                        physical_savings_require_separate_verified_compaction=True)
        finally:
            disk.close()
