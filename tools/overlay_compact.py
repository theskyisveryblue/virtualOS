"""Create a compact, verified copy of a closed overlay; never replace its source."""
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import struct
import subprocess
import tempfile

from cloud_disk import BLOCK


def no_open_files(path):
    row = subprocess.run(['lsof', '-nP', '--', str(path)], capture_output=True, timeout=10)
    return row.returncode == 1 and not row.stdout and not row.stderr


def regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise ValueError('owned unlinked regular file without shared write access required')
    return info


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def fingerprint(database):
    result = hashlib.sha256(b'virtualos-overlay-logical/v1\0')
    for key, value in database.execute('SELECT name,value FROM metadata ORDER BY name'):
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError('invalid overlay metadata')
        result.update(b'M')
        for item in (key.encode(), value.encode()):
            result.update(struct.pack('>Q', len(item))); result.update(item)
    count = 0
    for index, data in database.execute('SELECT idx,data FROM blocks ORDER BY idx'):
        if type(index) is not int or index < 0 or not isinstance(data, bytes) or not 0 < len(data) <= BLOCK:
            raise ValueError('invalid overlay block')
        result.update(b'B'); result.update(struct.pack('>QQ', index, len(data))); result.update(data)
        count += 1
    return result.hexdigest(), count


def compact_copy(source, destination, *, probe=no_open_files, reserve=10 * 1024**3):
    source, destination = Path(source), Path(destination)
    for path in (source, destination):
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('literal absolute paths required')
        for parent in path.parents:
            if parent.is_symlink():
                raise ValueError('linked parent refused')
        production = Path.home() / 'VirtualOSMachines'
        if production in path.parents:
            raise ValueError('production store refused')
    before = regular(source)
    source_parent = source.parent.lstat()
    if (not stat.S_ISDIR(source_parent.st_mode) or source_parent.st_uid != os.getuid()
            or source_parent.st_mode & 0o077):
        raise ValueError('source parent must be private and owned')
    parent = destination.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077
            or destination.exists() or destination.is_symlink()):
        raise ValueError('new destination in private owned directory required')
    if type(reserve) is not int or reserve < 0 or not 0 < before.st_size <= 4 * 1024**3:
        raise ValueError('invalid reserve or oversized overlay')
    lock_path = source.with_suffix('.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'r+b') as lock:
        regular(lock_path)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in (source, *(Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
            if path.exists() or path.is_symlink():
                info = regular(path)
                # Old macOS SQLite can leave its unused WAL-index SHM after
                # a clean close. Immutable reads ignore it; only journals can
                # contain data not represented by the main database.
                if str(path).endswith(('-wal', '-journal')) and info.st_size:
                    raise ValueError('overlay must be cleanly closed without pending journals')
                if not probe(path):
                    raise ValueError('overlay open-file check did not prove it unused')
        if identity(regular(source)) != identity(before):
            raise ValueError('overlay changed during admission')
        free = os.statvfs(destination.parent)
        if free.f_bavail * free.f_frsize < reserve + 2 * before.st_size + 4 * 1024**2:
            raise OSError('insufficient compaction headroom')
        descriptor, name = tempfile.mkstemp(prefix='.overlay-compact-', dir=destination.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            reader = sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)
            try:
                if reader.execute('PRAGMA quick_check').fetchone() != ('ok',):
                    raise ValueError('overlay integrity check failed')
                objects = reader.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
                if set(objects) != {('table', 'blocks'), ('table', 'metadata')}:
                    raise ValueError('unexpected overlay schema objects')
                if [row[1] for row in reader.execute('PRAGMA table_info(blocks)')] != ['idx', 'data'] or \
                   [row[1] for row in reader.execute('PRAGMA table_info(metadata)')] != ['name', 'value']:
                    raise ValueError('unexpected overlay columns')
                expected, count = fingerprint(reader)
                writer = sqlite3.connect(temporary)
                try:
                    writer.execute('PRAGMA synchronous=FULL')
                    writer.execute('CREATE TABLE blocks (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)')
                    writer.execute('CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    with writer:
                        writer.executemany('INSERT INTO metadata VALUES (?,?)', reader.execute('SELECT name,value FROM metadata'))
                        writer.executemany('INSERT INTO blocks VALUES (?,?)', reader.execute('SELECT idx,data FROM blocks ORDER BY idx'))
                    if fingerprint(writer) != (expected, count):
                        raise ValueError('compacted overlay logical digest mismatch')
                finally:
                    writer.close()
            finally:
                reader.close()
            if identity(regular(source)) != identity(before):
                raise ValueError('source changed during compaction')
            after = regular(temporary)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.link(temporary, destination)  # Atomic publication without overwriting anything.
            temporary.unlink()
            folder = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
            return dict(source_retained=True, logical_contents_verified=True, blocks=count,
                        logical_sha256=expected, source_bytes=before.st_size,
                        compacted_bytes=after.st_size, candidate_bytes_saved=before.st_size-after.st_size,
                        source_allocated_bytes=before.st_blocks * 512,
                        compacted_allocated_bytes=after.st_blocks * 512,
                        original_replaced=False)
        finally:
            temporary.unlink(missing_ok=True)
