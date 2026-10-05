"""Immutable chunk disks, durable overlays, and loopback NBD lab servers.

The isolated clone lab reads only the APFS staging copy. A separate pinned R2
GET path can supply immutable chunks; neither path opens the production RAW.
"""

import hashlib
import errno
import json
import os
from pathlib import Path
import random
import re
import socket
import sqlite3
import stat as stat_module
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib


CHUNK = 4 * 1024 * 1024
BLOCK = 64 * 1024
MAX_LOGICAL_BYTES = 64 * 1024**3
MAX_MANIFEST_BYTES = 16 * 1024**2
BOOT_OVERLAY_RESERVE_BYTES = 512 * 1024**2
FORMAT = "virtualos-cloud-disk/v1"
CONFIG_FORMAT = "virtualos-cloud-disk-config/v1"
CLOUD_DISK_BUCKET = "virtualos-cloud-disks"
NBD_MAGIC = 0x4E42444D41474943
NBD_OPTION_MAGIC = 0x49484156454F5054
NBD_OPTION_REPLY_MAGIC = 0x0003E889045565A9
NBD_REQUEST_MAGIC = 0x25609513
NBD_SIMPLE_REPLY_MAGIC = 0x67446698
NBD_TRANSMISSION_FLAGS = 1 | 4 | 8  # HAS_FLAGS, SEND_FLUSH, SEND_FUA


class BoundedCounter:
    """Constant-space power-of-two histogram; never retains request data."""

    def __init__(self):
        self.count = 0
        self.total = 0
        self.maximum = 0
        self.buckets = [0] * 33

    def add(self, value):
        value = max(0, int(value))
        self.count += 1
        self.total += value
        self.maximum = max(self.maximum, value)
        self.buckets[min((max(1, value) - 1).bit_length(), 32)] += 1

    def percentile_upper(self, numerator, denominator=100):
        if not self.count:
            return None
        rank = (self.count * numerator + denominator - 1) // denominator
        seen = 0
        for exponent, count in enumerate(self.buckets):
            seen += count
            if seen >= rank:
                return 1 << exponent
        raise AssertionError("histogram count mismatch")

    def summary(self, unit):
        return {"count": self.count, f"{unit}_total": self.total,
                f"{unit}_max": self.maximum,
                f"{unit}_p50_upper": self.percentile_upper(50),
                f"{unit}_p95_upper": self.percentile_upper(95),
                f"{unit}_p99_upper": self.percentile_upper(99)}


class NBDRequestMetrics:
    """Per-operation aggregate sizes and backend time, excluding guest contents."""

    def __init__(self):
        self.operations = {name: {"sizes": BoundedCounter(), "latency": BoundedCounter(),
                                  "errors": 0} for name in ("read", "write", "flush")}

    def record(self, name, size, elapsed_ns, error):
        operation = self.operations[name]
        operation["sizes"].add(size)
        operation["latency"].add((max(0, elapsed_ns) + 999) // 1000)
        operation["errors"] += int(error != 0)

    def summary(self):
        return {name: {"errors": operation["errors"],
                       **operation["sizes"].summary("request_bytes"),
                       **operation["latency"].summary("latency_us")}
                for name, operation in self.operations.items()}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_config(path):
    config = json.loads(Path(path).read_text())
    required = {"schema", "account_id", "bucket", "endpoint", "object_prefix",
                "storage_class", "chunk_size_bytes", "block_size_bytes",
                "local_cache_limit_bytes", "local_overlay_limit_bytes",
                "boot_cache_required", "credentials"}
    if set(config) != required or config["schema"] != CONFIG_FORMAT:
        raise ValueError("invalid config schema or fields")
    account = config["account_id"]
    if not isinstance(account, str) or not re.fullmatch(r"[0-9a-f]{32}", account):
        raise ValueError("valid cloud disk account ID required")
    if config["endpoint"] != "https://" + account + ".r2.cloudflarestorage.com":
        raise ValueError("endpoint does not match account ID")
    if config["bucket"] != CLOUD_DISK_BUCKET:
        raise ValueError("dedicated virtualos-cloud-disks bucket required")
    if not re.fullmatch(r"[a-z0-9-]+(?:/[a-z0-9-]+)*", config["object_prefix"]):
        raise ValueError("invalid object prefix")
    if (config["storage_class"] != "Standard" or config["chunk_size_bytes"] != CHUNK or
            config["block_size_bytes"] != BLOCK or config["credentials"] != "environment_only" or
            config["boot_cache_required"] is not True):
        raise ValueError("unsupported cloud disk settings")
    for key in ("local_cache_limit_bytes", "local_overlay_limit_bytes"):
        if type(config[key]) is not int or not CHUNK <= config[key] <= 64 * 1024**3:
            raise ValueError("invalid local storage limit")
    return config


def atomic_write(path, data):
    temporary = path.with_name(path.name + ".next." + str(os.getpid()))
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def pack(source, directory):
    source, directory = Path(source), Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("destination must be empty")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    objects = directory / "objects"
    objects.mkdir(mode=0o700)
    entries = []
    logical_size = source.stat().st_size
    with source.open("rb", buffering=0) as stream:
        index = 0
        while raw := stream.read(CHUNK):
            if raw.count(0) == len(raw):
                entries.append({"zero": True, "raw_size": len(raw)})
            else:
                encoded = zlib.compress(raw, level=1)
                key = digest(encoded)
                target = objects / (key + ".z")
                if not target.exists():
                    atomic_write(target, encoded)
                entries.append({"key": key, "sha256": digest(raw),
                                "raw_size": len(raw), "encoded_size": len(encoded)})
            index += 1
    manifest = {"format": FORMAT, "logical_size": logical_size,
                "chunk_size": CHUNK, "block_size": BLOCK, "chunks": entries}
    atomic_write(directory / "manifest.json",
                 (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return manifest


class CloudDisk:
    """Immutable local objects plus SQLite WAL block overlay for a future NBD server."""

    def __init__(self, directory, overlay, fetch=None, cache_dir=None,
                 cache_limit_bytes=4 * 1024**3,
                 overlay_limit_bytes=4 * 1024**3, min_host_free_bytes=512 * 1024**2,
                 cache_write_reserve_bytes=0):
        self.directory = Path(directory)
        with (self.directory / "manifest.json").open("rb") as stream:
            manifest_bytes = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("cloud disk manifest exceeds size bound")
        self.manifest = json.loads(manifest_bytes)
        manifest_hash = digest(manifest_bytes)
        m = self.manifest
        if (not isinstance(m, dict) or set(m) != {"format", "logical_size", "chunk_size",
                                              "block_size", "chunks"} or
                m["format"] != FORMAT or m["chunk_size"] != CHUNK or
                m["block_size"] != BLOCK or type(m["logical_size"]) is not int or
                not 512 <= m["logical_size"] <= MAX_LOGICAL_BYTES or
                m["logical_size"] % 512 or not isinstance(m["chunks"], list) or
                len(m["chunks"]) != (m["logical_size"] + CHUNK - 1) // CHUNK):
            raise ValueError("invalid cloud disk manifest")
        for index, entry in enumerate(m["chunks"]):
            expected = min(CHUNK, m["logical_size"] - index * CHUNK)
            if not isinstance(entry, dict) or type(entry.get("raw_size")) is not int or entry["raw_size"] != expected:
                raise ValueError("invalid cloud disk chunk entry")
            if entry.get("zero") is True:
                if set(entry) != {"zero", "raw_size"}:
                    raise ValueError("invalid zero chunk entry")
            elif (set(entry) != {"key", "sha256", "raw_size", "encoded_size"} or
                  not all(isinstance(entry[name], str) and
                          re.fullmatch(r"[0-9a-f]{64}", entry[name])
                          for name in ("key", "sha256")) or
                  type(entry["encoded_size"]) is not int or
                  not 1 <= entry["encoded_size"] <= CHUNK + 65536):
                raise ValueError("invalid compressed chunk entry")
        self.size = m["logical_size"]
        self.cache = {}
        self.fetch = fetch
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.cache_limit_bytes = cache_limit_bytes
        self.overlay_limit_bytes = overlay_limit_bytes
        self.min_host_free_bytes = min_host_free_bytes
        self.cache_write_reserve_bytes = cache_write_reserve_bytes
        if self.cache_dir is not None:
            self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._open_overlay(overlay, manifest_hash)

    def _open_overlay(self, overlay, identity_hash):
        self.overlay_limit_bytes = getattr(self, "overlay_limit_bytes", 4 * 1024**3)
        self.min_host_free_bytes = getattr(self, "min_host_free_bytes", 512 * 1024**2)
        overlay = Path(overlay)
        self.overlay_path = overlay
        overlay.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(overlay), timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS blocks (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        identity = self.db.execute("SELECT value FROM metadata WHERE name='manifest_sha256'").fetchone()
        if identity is not None and identity[0] != identity_hash:
            self.db.close()
            raise ValueError("overlay belongs to a different immutable base")
        if identity is None:
            self.db.execute("INSERT INTO metadata(name,value) VALUES ('manifest_sha256',?)",
                            (identity_hash,))
        self.db.commit()

    def _encoded(self, key):
        if self.cache_dir is not None:
            target = self.cache_dir / (key + ".z")
            if target.exists():
                if target.stat().st_size > CHUNK + 65536:
                    target.unlink()
                else:
                    encoded = target.read_bytes()
                    if digest(encoded) == key:
                        os.utime(target, None)
                        return encoded
                    target.unlink()
        if self.fetch is None:
            encoded = (self.directory / "objects" / (key + ".z")).read_bytes()
        else:
            encoded = self.fetch(key)
        if not isinstance(encoded, bytes) or len(encoded) > CHUNK + 65536 or digest(encoded) != key:
            raise ValueError("cloud disk object hash mismatch")
        if self.cache_dir is not None and len(encoded) <= self.cache_limit_bytes:
            # A cache miss must never consume the free-space reserve needed by
            # the durable overlay. The fetched object is still usable in RAM.
            free = os.statvfs(self.cache_dir)
            available = free.f_bavail * free.f_frsize
            if available - len(encoded) >= self.min_host_free_bytes + self.cache_write_reserve_bytes:
                target = self.cache_dir / (key + ".z")
                try:
                    atomic_write(target, encoded)
                except OSError as failure:
                    if failure.errno not in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG):
                        raise
                else:
                    self._trim_cache(protected=target)
        return encoded

    def _trim_cache(self, protected):
        objects = sorted(self.cache_dir.glob("*.z"), key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in objects)
        for path in objects:
            if total <= self.cache_limit_bytes:
                break
            if path == protected:
                continue
            size = path.stat().st_size
            path.unlink()
            total -= size

    def _ensure_host_space(self, projected_bytes):
        def available():
            stat = os.statvfs(str(self.overlay_path.parent))
            return stat.f_bavail * stat.f_frsize

        required = self.min_host_free_bytes + projected_bytes
        if available() >= required:
            return
        if self.cache_dir is not None:
            candidates = sorted((path for path in self.cache_dir.glob("*.z")
                                 if not path.is_symlink() and path.is_file()),
                                key=lambda path: path.stat().st_mtime)
            for path in candidates:
                path.unlink()
                if available() >= required:
                    return
        raise OSError(errno.ENOSPC, "host free space guard reached")

    def _chunk(self, index):
        if index in self.cache:
            return self.cache[index]
        entry = self.manifest["chunks"][index]
        expected = min(CHUNK, self.size - index * CHUNK)
        if entry.get("raw_size") != expected:
            raise ValueError("invalid chunk length")
        if entry.get("zero") is True:
            raw = bytes(expected)
        else:
            key = entry.get("key", "")
            if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
                raise ValueError("invalid object key")
            encoded_size = entry.get("encoded_size")
            if type(encoded_size) is not int or not 1 <= encoded_size <= CHUNK + 65536:
                raise ValueError("invalid compressed chunk length")
            encoded = self._encoded(key)
            # _encoded verifies the SHA-256 on every source, including cache hits.
            if len(encoded) != encoded_size:
                raise ValueError("cloud disk object hash mismatch")
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(encoded, expected + 1)
            if (len(raw) != expected or not decompressor.eof or
                    decompressor.unconsumed_tail or decompressor.unused_data or
                    digest(raw) != entry.get("sha256")):
                raise ValueError("cloud disk raw hash mismatch")
        if len(self.cache) >= 2:
            self.cache.pop(next(iter(self.cache)))
        self.cache[index] = raw
        return raw

    def _base_block(self, index):
        offset = index * BLOCK
        if offset >= self.size:
            raise ValueError("block beyond disk")
        chunk = self._chunk(offset // CHUNK)
        start = offset % CHUNK
        return chunk[start:start + min(BLOCK, self.size - offset)]

    def _block(self, index):
        row = self.db.execute("SELECT data FROM blocks WHERE idx=?", (index,)).fetchone()
        if row is None:
            return self._base_block(index)
        expected = min(BLOCK, self.size - index * BLOCK)
        if len(row[0]) != expected:
            raise ValueError("overlay block length mismatch")
        return row[0]

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise ValueError("read beyond disk")
        output = bytearray()
        while length:
            index, start = divmod(offset, BLOCK)
            piece = self._block(index)[start:start + length]
            output.extend(piece)
            offset += len(piece)
            length -= len(piece)
        return bytes(output)

    def write(self, offset, data):
        if offset < 0 or offset + len(data) > self.size:
            raise ValueError("write beyond disk")
        if data:
            first = offset // BLOCK
            last = (offset + len(data) - 1) // BLOCK
            touched = last - first + 1
            existing_touched = self.db.execute(
                "SELECT COUNT(*) FROM blocks WHERE idx BETWEEN ? AND ?", (first, last)).fetchone()[0]
            existing_total = self.db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
            if (existing_total + touched - existing_touched) * BLOCK > self.overlay_limit_bytes:
                raise OSError(errno.ENOSPC, "cloud disk overlay limit reached")
            # SQLite can write full replacement blocks into both the WAL and
            # main database. Reserve for this request before starting it.
            projected_bytes = touched * BLOCK * 2 + 4 * 1024**2
            self._ensure_host_space(projected_bytes)
        position = 0
        with self.db:
            while position < len(data):
                index, start = divmod(offset + position, BLOCK)
                block_size = min(BLOCK, self.size - index * BLOCK)
                count = min(len(data) - position, block_size - start)
                if start == 0 and count == block_size:
                    # A complete replacement needs no base chunk or R2 GET.
                    current = data[position:position + count]
                else:
                    current = bytearray(self._block(index))
                    current[start:start + count] = data[position:position + count]
                self.db.execute("INSERT OR REPLACE INTO blocks(idx,data) VALUES (?,?)",
                                (index, bytes(current)))
                position += count

    def flush(self):
        self.db.commit()
        result = self.db.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
        if result is None or result[0] != 0:
            raise sqlite3.OperationalError("cloud disk WAL checkpoint incomplete")

    def close(self):
        try:
            self.flush()
        finally:
            self.db.close()


def open_r2_disk(config_path, manifest_sha256, overlay, cache_dir,
                 *, min_host_free_bytes=10 * 1024**3, transport=None,
                 cache_write_reserve_bytes=BOOT_OVERLAY_RESERVE_BYTES):
    """Open a pinned immutable R2 base with a local durable overlay."""
    from cloud_disk_r2 import R2ReadOnly

    config = validate_config(config_path)
    source = R2ReadOnly(config, manifest_sha256, transport=transport)
    cache_dir = Path(cache_dir)
    base_dir = cache_dir / "base"
    manifest_path = base_dir / "manifest.json"
    objects_dir = cache_dir / "objects"
    if any(path.is_symlink() for path in (cache_dir, base_dir, manifest_path,
                                          objects_dir, Path(overlay))):
        raise ValueError("R2 cache and overlay must not contain symlinks")
    base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    local_manifest = b""
    if manifest_path.is_file():
        with manifest_path.open("rb") as stream:
            local_manifest = stream.read(MAX_MANIFEST_BYTES + 1)
    if digest(local_manifest) != manifest_sha256:
        manifest_bytes = source.fetch_manifest()
        atomic_write(manifest_path, manifest_bytes)
    return CloudDisk(base_dir, overlay, fetch=source.fetch_chunk,
                     cache_dir=objects_dir,
                     cache_limit_bytes=config["local_cache_limit_bytes"],
                     overlay_limit_bytes=config["local_overlay_limit_bytes"],
                     min_host_free_bytes=min_host_free_bytes,
                     cache_write_reserve_bytes=max(0, min(cache_write_reserve_bytes,
                                                          config["local_overlay_limit_bytes"])))


class RawDisk(CloudDisk):
    """Read only RAW base with the same bounded, durable block overlay."""

    def __init__(self, source, overlay, overlay_limit_bytes=1024**3,
                 min_host_free_bytes=10 * 1024**3):
        self.source = Path(source)
        self.fd = os.open(self.source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            stat = os.fstat(self.fd)
            if not stat_module.S_ISREG(stat.st_mode) or stat.st_size < 512 or stat.st_size % 512:
                raise ValueError("RAW source must be a 512-byte-aligned regular file")
            self.size = stat.st_size
            self.source_identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            self.overlay_limit_bytes = overlay_limit_bytes
            self.min_host_free_bytes = min_host_free_bytes
            identity = digest(json.dumps([str(self.source.resolve()), *self.source_identity]).encode())
            self._open_overlay(overlay, identity)
        except BaseException:
            os.close(self.fd)
            raise

    def _base_block(self, index):
        offset = index * BLOCK
        if offset >= self.size:
            raise ValueError("block beyond disk")
        stat = os.fstat(self.fd)
        if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != self.source_identity:
            raise OSError(errno.EIO, "RAW base changed during export")
        expected = min(BLOCK, self.size - offset)
        data = os.pread(self.fd, expected, offset)
        if len(data) != expected:
            raise OSError(errno.EIO, "short RAW base read")
        return data

    def close(self):
        try:
            super().close()
        finally:
            os.close(self.fd)


def receive_exact(connection, count):
    data = bytearray()
    while len(data) < count:
        piece = connection.recv(count - len(data))
        if not piece:
            raise EOFError("NBD peer disconnected")
        data.extend(piece)
    return bytes(data)


def serve_nbd_connection(connection, directory, overlay, export=b"virtualos-test",
                         overlay_limit_bytes=4 * 1024**3, stats=None, disk_factory=None,
                         idle_timeout=10):
    """One loopback NBD connection for protocol testing; no public listener."""
    disk = (disk_factory() if disk_factory is not None else
            CloudDisk(directory, overlay, overlay_limit_bytes=overlay_limit_bytes))
    if stats is None:
        stats = {}
    stats.update(negotiated=False, requests=0, reads=0, writes=0, flushes=0)
    metrics = NBDRequestMetrics()
    # Keep negotiation bounded even when a long-lived VM session has no idle
    # timeout; an idle guest may legitimately issue no I/O for many minutes.
    connection.settimeout(300 if idle_timeout is None else idle_timeout)
    try:
        connection.sendall(struct.pack(">QQH", NBD_MAGIC, NBD_OPTION_MAGIC, 1))
        client_flags, = struct.unpack(">I", receive_exact(connection, 4))
        if client_flags & ~1:
            return
        while True:
            magic, option, length = struct.unpack(">QII", receive_exact(connection, 16))
            if magic != NBD_OPTION_MAGIC or length > 4096:
                return
            data = receive_exact(connection, length)
            if option == 1:  # NBD_OPT_EXPORT_NAME
                if data != export:
                    return
                connection.sendall(struct.pack(">QH", disk.size, NBD_TRANSMISSION_FLAGS)
                                   + bytes(124))
                stats["negotiated"] = True
                break
            if option in (6, 7):  # NBD_OPT_INFO, NBD_OPT_GO
                if len(data) < 6:
                    return
                name_length, = struct.unpack(">I", data[:4])
                if name_length > len(data) - 6:
                    return
                name = data[4:4 + name_length]
                tail = data[4 + name_length:]
                requested_count, = struct.unpack(">H", tail[:2])
                if len(tail) != 2 + requested_count * 2:
                    return
                if name != export:
                    connection.sendall(struct.pack(">QIII", NBD_OPTION_REPLY_MAGIC,
                                                   option, 0x80000006, 0))
                    continue
                info = struct.pack(">HQH", 0, disk.size, NBD_TRANSMISSION_FLAGS)
                connection.sendall(struct.pack(">QIII", NBD_OPTION_REPLY_MAGIC,
                                               option, 3, len(info)) + info)
                requests = struct.unpack(">" + "H" * requested_count, tail[2:]) if requested_count else ()
                if 3 in requests:  # NBD_INFO_BLOCK_SIZE, default constraints
                    sizes = struct.pack(">HIII", 3, 1, 4096, 32 * 1024 * 1024)
                    connection.sendall(struct.pack(">QIII", NBD_OPTION_REPLY_MAGIC,
                                                   option, 3, len(sizes)) + sizes)
                connection.sendall(struct.pack(">QIII", NBD_OPTION_REPLY_MAGIC,
                                               option, 1, 0))
                if option == 7:
                    stats["negotiated"] = True
                    break
                continue
            if option == 2:  # NBD_OPT_ABORT
                connection.sendall(struct.pack(">QIII", NBD_OPTION_REPLY_MAGIC,
                                               option, 1, 0))
                return
            connection.sendall(struct.pack(">QIII", NBD_OPTION_REPLY_MAGIC,
                                           option, 0x80000001, 0))
        connection.settimeout(idle_timeout)
        while True:
            header = receive_exact(connection, 28)
            magic, flags, command, cookie, offset, length = struct.unpack(">IHHQQI", header)
            if magic != NBD_REQUEST_MAGIC or length > 32 * 1024 * 1024:
                return
            if command == 2:  # NBD_CMD_DISC has no reply
                return
            stats["requests"] += 1
            payload = receive_exact(connection, length) if command == 1 else b""
            operation = {0: "read", 1: "write", 3: "flush"}.get(command)
            started_ns = time.perf_counter_ns() if operation is not None else None
            error, answer = 0, b""
            if flags & ~1:
                error = 22
            elif offset + length > disk.size:
                error = 28 if command == 1 else 22
            else:
                try:
                    if command == 0:
                        answer = disk.read(offset, length)
                        stats["reads"] += 1
                    elif command == 1:
                        disk.write(offset, payload)
                        stats["writes"] += 1
                        if flags & 1:
                            disk.flush()
                    elif command == 3 and offset == 0 and length == 0:
                        disk.flush()
                        stats["flushes"] += 1
                    else:
                        error = 22
                except OSError as failure:
                    error = 28 if failure.errno in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG) else 5
                except (ValueError, sqlite3.Error, zlib.error):
                    error = 5  # NBD_EIO; never acknowledge a failed write
            if operation is not None:
                metrics.record(operation, length, time.perf_counter_ns() - started_ns, error)
            connection.sendall(struct.pack(">IIQ", NBD_SIMPLE_REPLY_MAGIC, error, cookie) + answer)
    except (EOFError, OSError, socket.timeout):
        pass
    finally:
        stats["metrics"] = metrics.summary()
        try:
            disk.close()
        finally:
            connection.close()


def serve_nbd_sessions(listener, directory, overlay, *, disk_factory=None,
                       max_connections=8, first_timeout=180, reconnect_timeout=45,
                       idle_timeout=300, event_name=None):
    """Serve bounded sequential reconnects against one durable overlay."""
    if not 1 <= max_connections <= 8 or first_timeout <= 0 or reconnect_timeout <= 0:
        raise ValueError("invalid NBD session limits")
    sessions = []
    for sequence in range(max_connections):
        listener.settimeout(first_timeout if sequence == 0 else reconnect_timeout)
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            if sequence == 0:
                raise
            break
        stats = {}
        serve_nbd_connection(connection, directory, overlay, stats=stats,
                             disk_factory=disk_factory, idle_timeout=idle_timeout)
        stats["sequence"] = sequence + 1
        sessions.append(stats)
        if event_name is not None:
            print(json.dumps({event_name: stats}, sort_keys=True), flush=True)
    return sessions


def self_test():
    with tempfile.TemporaryDirectory(prefix="vos-cloud-disk-", dir="/private/tmp") as temp:
        root = Path(temp)
        source = root / "source.raw"
        payload = bytes(range(256)) * 256
        with source.open("wb") as stream:
            stream.write(payload)
            stream.write(bytes(CHUNK - len(payload)))
            stream.write(bytes(CHUNK))
            stream.write(payload)
            stream.write(bytes(123))
            stream.write(bytes((-stream.tell()) % 512))
        original = source.read_bytes()
        raw_overlay = root / "raw-overlay.sqlite"
        raw = RawDisk(source, raw_overlay, overlay_limit_bytes=BLOCK,
                      min_host_free_bytes=0)
        assert raw.read(0, len(original)) == original
        raw.write(123, b"raw-overlay")
        raw.flush()
        raw.close()
        assert source.read_bytes() == original
        raw = RawDisk(source, raw_overlay, overlay_limit_bytes=BLOCK,
                      min_host_free_bytes=0)
        assert raw.read(123, 11) == b"raw-overlay"
        try:
            raw.write(BLOCK, b"second block")
            raise AssertionError("RAW overlay limit was ignored")
        except OSError as error:
            assert error.errno == errno.ENOSPC
        raw.close()
        manifest = pack(source, root / "disk")
        malformed_root = root / "malformed"
        malformed_root.mkdir()
        for mutation in ("oversized", "missing-hash", "extra-zero-field", "bool-size"):
            bad = json.loads(json.dumps(manifest))
            if mutation == "oversized":
                bad["logical_size"] = MAX_LOGICAL_BYTES + 512
            elif mutation == "missing-hash":
                del bad["chunks"][0]["sha256"]
            elif mutation == "extra-zero-field":
                bad["chunks"][1]["key"] = "0" * 64
            else:
                bad["chunks"][0]["raw_size"] = True
            (malformed_root / "manifest.json").write_text(json.dumps(bad))
            try:
                CloudDisk(malformed_root, root / "should-not-open.sqlite")
                raise AssertionError(f"malformed manifest accepted: {mutation}")
            except ValueError:
                pass
        assert not (root / "should-not-open.sqlite").exists()
        from cloud_disk_r2 import R2ReadOnly, self_test as r2_self_test
        from unittest.mock import patch as mock_patch
        fake_account = "f" * 32
        fake_config = {"schema": CONFIG_FORMAT, "account_id": fake_account,
                       "bucket": CLOUD_DISK_BUCKET, "endpoint":
                       f"https://{fake_account}.r2.cloudflarestorage.com",
                       "object_prefix": "disks/test", "storage_class": "Standard",
                       "chunk_size_bytes": CHUNK, "block_size_bytes": BLOCK,
                       "local_cache_limit_bytes": CHUNK,
                       "local_overlay_limit_bytes": CHUNK,
                       "boot_cache_required": True, "credentials": "environment_only"}
        fake_config_path = root / "r2-config.json"
        fake_config_path.write_text(json.dumps(fake_config))
        manifest_sha256 = digest((root / "disk" / "manifest.json").read_bytes())
        def test_r2_disk(fake_transport):
            remote_disk = open_r2_disk(fake_config_path, manifest_sha256,
                                       root / "r2-overlay.sqlite", root / "r2-cache",
                                       min_host_free_bytes=0, transport=fake_transport)
            assert remote_disk.read(0, 16) == original[:16]
            remote_disk.close()
        with mock_patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": fake_account,
                                     "CLOUDFLARE_R2_ENDPOINT": fake_config["endpoint"],
                                     "AWS_ACCESS_KEY_ID": "fake-access-key",
                                     "AWS_SECRET_ACCESS_KEY": "fake-secret-key"}):
            assert r2_self_test(root / "disk", fake_config, manifest_sha256,
                                open_disk=test_r2_disk)
        from cloud_disk_r2_upload import R2UploadStore, self_test as r2_upload_self_test
        assert r2_upload_self_test(root)
        ai_account = "c24a3fb4bedba669785c18c834f51921"
        ai_config = dict(fake_config, account_id=ai_account,
                         endpoint=f"https://{ai_account}.r2.cloudflarestorage.com")
        ai_config_path = root / "ai-r2-config.json"
        ai_config_path.write_text(json.dumps(ai_config))
        with mock_patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": ai_account,
                                     "CLOUDFLARE_R2_ENDPOINT": ai_config["endpoint"],
                                     "AWS_ACCESS_KEY_ID": "fake-access-key",
                                     "AWS_SECRET_ACCESS_KEY": "fake-secret-key"}):
            assert validate_config(ai_config_path)["bucket"] == CLOUD_DISK_BUCKET
            assert R2ReadOnly(ai_config, "0" * 64).bucket == CLOUD_DISK_BUCKET
            assert R2UploadStore(ai_config).bucket == CLOUD_DISK_BUCKET
            wrong_bucket = dict(ai_config, bucket="virtualos-vm-backups")
            ai_config_path.write_text(json.dumps(wrong_bucket))
            try:
                validate_config(ai_config_path)
                raise AssertionError("VM backup bucket accepted for cloud disk")
            except ValueError:
                pass
            try:
                R2ReadOnly(wrong_bucket, "0" * 64)
                raise AssertionError("VM backup bucket accepted for R2 reads")
            except ValueError:
                pass
        overlay = root / "overlay" / "blocks.sqlite"
        disk = CloudDisk(root / "disk", overlay)
        assert disk.read(0, len(original)) == original
        assert disk.read(CHUNK - 10, 20) == bytes(20)
        patch = b"durable-overlay" * 5000
        offset = CHUNK - 17
        disk.write(offset, patch)
        disk.flush()
        disk.close()
        expected = bytearray(original)
        expected[offset:offset + len(patch)] = patch
        disk = CloudDisk(root / "disk", overlay)
        assert disk.read(offset - 1, len(patch) + 2) == expected[offset - 1:offset + len(patch) + 1]
        assert disk.read(0, len(expected)) == expected
        try:
            disk.read(len(expected) - 1, 2)
            raise AssertionError("out-of-range read accepted")
        except ValueError:
            pass
        disk.close()
        cache_dir = root / "cache"
        fetches = []
        def local_fetch(key):
            fetches.append(key)
            return (root / "disk" / "objects" / (key + ".z")).read_bytes()
        disk = CloudDisk(root / "disk", overlay, fetch=local_fetch,
                         cache_dir=cache_dir, cache_limit_bytes=CHUNK)
        assert disk.read(0, 1) == expected[:1]
        assert len(fetches) == 1
        disk.close()
        def offline_fetch(_key):
            raise OSError("simulated remote outage")
        disk = CloudDisk(root / "disk", overlay, fetch=offline_fetch,
                         cache_dir=cache_dir, cache_limit_bytes=CHUNK)
        assert disk.read(0, 1) == expected[:1]
        disk.close()
        guarded_cache = root / "guarded-cache"
        free = os.statvfs(root)
        reserve_above_free = free.f_bavail * free.f_frsize + CHUNK
        disk = CloudDisk(root / "disk", overlay, fetch=local_fetch,
                         cache_dir=guarded_cache, cache_limit_bytes=CHUNK,
                         min_host_free_bytes=reserve_above_free)
        assert disk.read(0, 1) == expected[:1]
        assert not list(guarded_cache.iterdir())
        disk.close()
        client, server = socket.socketpair()
        socket_stats = {}
        worker = threading.Thread(target=serve_nbd_connection,
                                  args=(server, root / "disk", overlay),
                                  kwargs={"stats": socket_stats,
                                          "idle_timeout": None}, daemon=True)
        worker.start()
        wire_magic, option_magic, handshake_flags = struct.unpack(">QQH", receive_exact(client, 18))
        assert (wire_magic, option_magic, handshake_flags) == (NBD_MAGIC, NBD_OPTION_MAGIC, 1)
        client.sendall(struct.pack(">I", 1))
        export_name = b"virtualos-test"
        go = struct.pack(">I", len(export_name)) + export_name + struct.pack(">HH", 1, 3)
        client.sendall(struct.pack(">QII", NBD_OPTION_MAGIC, 7, len(go)) + go)
        info_types = []
        while True:
            reply_magic, option, reply_type, length = struct.unpack(">QIII", receive_exact(client, 20))
            assert reply_magic == NBD_OPTION_REPLY_MAGIC and option == 7
            detail = receive_exact(client, length)
            if reply_type == 1:
                break
            assert reply_type == 3
            info_types.append(struct.unpack(">H", detail[:2])[0])
        assert set(info_types) == {0, 3}
        def nbd_request(command, cookie, offset=0, data=b"", length=None, flags=0):
            if length is None:
                length = len(data)
            client.sendall(struct.pack(">IHHQQI", NBD_REQUEST_MAGIC, flags,
                                       command, cookie, offset, length) +
                           (data if command == 1 else b""))
            if command == 2:
                return None
            reply_magic, error, received_cookie = struct.unpack(">IIQ", receive_exact(client, 16))
            assert reply_magic == NBD_SIMPLE_REPLY_MAGIC and received_cookie == cookie
            return error, receive_exact(client, length) if command == 0 and error == 0 else b""
        assert nbd_request(0, 1, 0, length=8) == (0, expected[:8])
        assert server.gettimeout() is None
        assert nbd_request(1, 2, 1234, b"nbd-durable", flags=1) == (0, b"")
        assert nbd_request(3, 3) == (0, b"")
        assert nbd_request(0, 4, 1234, length=11) == (0, b"nbd-durable")
        assert nbd_request(0, 5, len(expected) - 1, length=2)[0] == 22
        nbd_request(2, 6)
        client.close()
        worker.join(timeout=10)
        assert not worker.is_alive()
        measurements = socket_stats["metrics"]
        assert measurements["read"]["count"] == 3
        assert measurements["write"]["count"] == 1
        assert measurements["flush"]["count"] == 1
        assert measurements["read"]["errors"] == 1
        assert measurements["read"]["request_bytes_total"] == 21
        assert measurements["write"]["request_bytes_total"] == 11
        assert measurements["read"]["latency_us_p99_upper"] is not None
        assert "offset" not in json.dumps(measurements)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            sessions = []
            def accept_twice():
                sessions.extend(serve_nbd_sessions(
                    listener, root / "disk", overlay, max_connections=2,
                    first_timeout=5, reconnect_timeout=5, idle_timeout=5))
            def connect_export():
                tcp = socket.create_connection(("127.0.0.1", port), timeout=5)
                assert struct.unpack(">QQH", receive_exact(tcp, 18))[0] == NBD_MAGIC
                tcp.sendall(struct.pack(">I", 1))
                tcp.sendall(struct.pack(">QII", NBD_OPTION_MAGIC, 1, len(export_name)) + export_name)
                size, flags = struct.unpack(">QH", receive_exact(tcp, 10))
                assert size == len(original) and flags == NBD_TRANSMISSION_FLAGS
                assert receive_exact(tcp, 124) == bytes(124)
                return tcp
            tcp_worker = threading.Thread(target=accept_twice, daemon=True)
            tcp_worker.start()
            with connect_export() as tcp:
                tcp.sendall(struct.pack(">IHHQQI", NBD_REQUEST_MAGIC, 1, 1, 77, 2345, 11)
                            + b"reconnectok")
                assert struct.unpack(">IIQ", receive_exact(tcp, 16)) == (NBD_SIMPLE_REPLY_MAGIC, 0, 77)
                tcp.sendall(struct.pack(">IHHQQI", NBD_REQUEST_MAGIC, 0, 2, 78, 0, 0))
            with connect_export() as tcp:
                tcp.sendall(struct.pack(">IHHQQI", NBD_REQUEST_MAGIC, 0, 0, 79, 2345, 11))
                assert struct.unpack(">IIQ", receive_exact(tcp, 16)) == (NBD_SIMPLE_REPLY_MAGIC, 0, 79)
                assert receive_exact(tcp, 11) == b"reconnectok"
                tcp.sendall(struct.pack(">IHHQQI", NBD_REQUEST_MAGIC, 0, 2, 80, 0, 0))
            tcp_worker.join(timeout=10)
            assert not tcp_worker.is_alive()
            assert len(sessions) == 2 and sessions[0]["writes"] == 1
            assert sessions[1]["reads"] == 1
        disk = CloudDisk(root / "disk", overlay)
        assert disk.read(1234, 11) == b"nbd-durable"
        disk.close()
        crash_overlay = root / "crash-overlay.sqlite"
        crash_log = root / "crash-server.log"
        crash_port = None
        for _ in range(40):
            candidate = random.randrange(20000, 61000)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", candidate))
                except OSError:
                    continue
                crash_port = candidate
                break
        assert crash_port is not None
        with crash_log.open("wb") as log:
            child = subprocess.Popen(
                [sys.executable, __file__, "crash-test-serve", str(root / "disk"),
                 str(crash_overlay), str(crash_port)], stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if '"ready"' in crash_log.read_text():
                        break
                    if child.poll() is not None:
                        raise AssertionError("disposable NBD crash server exited early")
                    time.sleep(0.02)
                else:
                    raise AssertionError("disposable NBD crash server did not start")
                with socket.create_connection(("127.0.0.1", crash_port), timeout=5) as tcp:
                    assert struct.unpack(">QQH", receive_exact(tcp, 18))[0] == NBD_MAGIC
                    tcp.sendall(struct.pack(">I", 1))
                    tcp.sendall(struct.pack(">QII", NBD_OPTION_MAGIC, 1,
                                            len(export_name)) + export_name)
                    assert struct.unpack(">QH", receive_exact(tcp, 10))[0] == len(original)
                    assert receive_exact(tcp, 124) == bytes(124)
                    tcp.sendall(struct.pack(">IHHQQI", NBD_REQUEST_MAGIC, 1, 1,
                                            91, 3456, 9) + b"crash-ack")
                    assert struct.unpack(">IIQ", receive_exact(tcp, 16)) == (
                        NBD_SIMPLE_REPLY_MAGIC, 0, 91)
                    assert child.poll() is None
                    child.kill()  # No NBD DISC or graceful SQLite close.
                    child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
        crashed = CloudDisk(root / "disk", crash_overlay)
        assert crashed.read(3456, 9) == b"crash-ack"
        crashed.close()
        fault_overlay = root / "outage-overlay.sqlite"
        faulted = CloudDisk(root / "disk", fault_overlay, fetch=offline_fetch,
                            min_host_free_bytes=0)
        try:
            faulted.write(100, b"failed-base")
            raise AssertionError("failed base GET acknowledged a write")
        except OSError:
            pass
        assert faulted.db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0
        faulted.fetch = lambda _key: b"malformed-object"
        try:
            faulted.write(100, b"failed-base")
            raise AssertionError("malformed base GET acknowledged a write")
        except ValueError:
            pass
        assert faulted.db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0
        faulted.close()
        recovered = CloudDisk(root / "disk", fault_overlay, fetch=local_fetch,
                              min_host_free_bytes=0)
        recovered.write(100, b"good")
        recovered.close()
        offline = CloudDisk(root / "disk", fault_overlay, fetch=offline_fetch,
                            min_host_free_bytes=0)
        assert offline.read(100, 4) == b"good"
        try:
            offline.read(BLOCK, 1)
            raise AssertionError("uncached base read succeeded during outage")
        except OSError:
            pass
        assert offline.read(100, 4) == b"good"
        offline.close()
        limited = CloudDisk(root / "disk", root / "limited.sqlite", overlay_limit_bytes=BLOCK)
        limited.write(0, b"a")
        try:
            limited.write(BLOCK, b"b")
            raise AssertionError("overlay limit was ignored")
        except OSError as error:
            assert error.errno == errno.ENOSPC
        assert limited.read(BLOCK, 1) == original[BLOCK:BLOCK + 1]
        limited.close()
        def offline_fetch(_key):
            raise OSError("base fetch should not occur for a complete block write")
        replacement = CloudDisk(root / "disk", root / "replacement.sqlite", fetch=offline_fetch)
        replacement.write(0, b"r" * BLOCK)
        assert replacement.read(0, BLOCK) == b"r" * BLOCK
        try:
            replacement.write(BLOCK, b"partial")
            raise AssertionError("partial block write skipped its base read")
        except OSError:
            pass
        replacement.close()
        from types import SimpleNamespace
        guarded = CloudDisk(root / "disk", root / "space-guard.sqlite",
                            min_host_free_bytes=BLOCK)
        projected = 2 * BLOCK + 4 * 1024**2
        with mock_patch("os.statvfs", return_value=SimpleNamespace(
                f_bavail=projected + BLOCK - 1, f_frsize=1)):
            try:
                guarded.write(0, b"x")
                raise AssertionError("write crossed host free-space reserve")
            except OSError as failure:
                assert failure.errno == errno.ENOSPC
        assert guarded.read(0, 1) == original[:1]
        guarded.close()
        eviction_cache = root / "eviction-cache"
        eviction_cache.mkdir()
        spare = eviction_cache / "spare.z"
        spare.write_bytes(b"cache")
        evicting = CloudDisk(root / "disk", root / "eviction-overlay.sqlite",
                             cache_dir=eviction_cache, min_host_free_bytes=BLOCK)
        def free_after_cache_eviction(_path):
            free_bytes = projected + BLOCK - (1 if spare.exists() else 0)
            return SimpleNamespace(f_bavail=free_bytes, f_frsize=1)
        with mock_patch("os.statvfs", side_effect=free_after_cache_eviction):
            evicting.write(0, b"e")
        assert not spare.exists() and evicting.read(0, 1) == b"e"
        evicting.close()
        from cloud_disk_checkpoint import (snapshot_overlay, restore_overlay,
                                           self_test_remote)
        checkpoint_dir = root / "overlay-checkpoint"
        checkpoint = snapshot_overlay(root / "disk", overlay, checkpoint_dir,
                                      max_bytes=1024**2)
        restored_overlay = root / "restored-overlay.sqlite"
        restored = restore_overlay(checkpoint_dir, root / "disk", restored_overlay,
                                   checkpoint["checkpoint_sha256"], max_bytes=1024**2)
        assert restored["restored_blocks"] == checkpoint["blocks"]
        source_disk = CloudDisk(root / "disk", overlay)
        restored_disk = CloudDisk(root / "disk", restored_overlay)
        assert source_disk.read(0, BLOCK * 2) == restored_disk.read(0, BLOCK * 2)
        assert source_disk.read(CHUNK - 17, BLOCK * 2) == restored_disk.read(CHUNK - 17, BLOCK * 2)
        source_disk.close()
        restored_disk.close()
        assert self_test_remote(root, root / "disk", overlay, checkpoint_dir,
                                checkpoint["checkpoint_sha256"], len(original))
        wrong_pin = root / "wrong-pin.sqlite"
        try:
            restore_overlay(checkpoint_dir, root / "disk", wrong_pin, "0" * 64,
                            max_bytes=1024**2)
            raise AssertionError("checkpoint accepted wrong content pin")
        except ValueError:
            pass
        assert not wrong_pin.exists()
        checkpoint_object = next((checkpoint_dir / "objects").glob("*.z"))
        checkpoint_object.write_bytes(b"BAD")
        corrupted_restore = root / "corrupted-restore.sqlite"
        try:
            restore_overlay(checkpoint_dir, root / "disk", corrupted_restore,
                            checkpoint["checkpoint_sha256"], max_bytes=1024**2)
            raise AssertionError("checkpoint accepted corrupt block object")
        except ValueError:
            pass
        assert not corrupted_restore.exists()
        first = next(entry for entry in manifest["chunks"] if "key" in entry)
        object_path = root / "disk" / "objects" / (first["key"] + ".z")
        with object_path.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"BAD")
        disk = CloudDisk(root / "disk", overlay)
        try:
            disk.read(BLOCK * 2, 1)
            raise AssertionError("corrupt object accepted")
        except ValueError:
            pass
        disk.close()
        manifest_path = root / "disk" / "manifest.json"
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        try:
            CloudDisk(root / "disk", overlay)
            raise AssertionError("overlay accepted a different manifest")
        except ValueError:
            pass
        changed_base_restore = root / "changed-base-restore.sqlite"
        try:
            restore_overlay(checkpoint_dir, root / "disk", changed_base_restore,
                            checkpoint["checkpoint_sha256"], max_bytes=1024**2)
            raise AssertionError("checkpoint accepted a different immutable base")
        except ValueError:
            pass
        assert not changed_base_restore.exists()
        temp_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        assert temp_bytes < 16 * 1024**2, "disposable disk self-test exceeded 16 MiB"
        print(json.dumps({"cloud_disk_self_test": "passed", "logical_bytes": len(original),
                          "temp_bytes_before_cleanup": temp_bytes,
                          "objects": sum("key" in item for item in manifest["chunks"]),
                          "zero_chunks": sum(item.get("zero", False) for item in manifest["chunks"]),
                          "overlay_survived_reopen": True, "cache_survived_offline": True,
                          "cache_respects_host_reserve": True,
                          "corruption_detected": True, "overlay_identity_checked": True,
                          "nbd_go_read_write_fua_flush": True,
                          "nbd_tcp_export_name": True,
                          "nbd_fua_reconnect_read": True,
                          "nbd_fua_ack_survives_sigkill": True,
                          "failed_base_get_preserves_overlay": True,
                          "r2_signed_read_only_fake_http": True,
                          "r2_stream_upload_fake_http": True,
                          "manifest_schema_and_size_bounds": True,
                          "reconnect_manifest_cache_reused": True,
                          "overlay_prewrite_reserve": True,
                          "full_block_write_skips_base_fetch": True,
                          "overlay_write_evicted_cache": True,
                          "overlay_checkpoint_restore_verified": True,
                          "remote_overlay_checkpoint_fake_r2_verified": True,
                          "overlay_limit_enforced": True}))


def lab_path(path):
    resolved = Path(path).resolve()
    if os.path.commonpath((str(resolved), "/private/tmp")) != "/private/tmp":
        raise ValueError("lab paths must remain under /private/tmp")
    return resolved


def lab_pack(source, destination):
    source, destination = lab_path(source), lab_path(destination)
    if not source.is_file() or source.is_symlink():
        raise ValueError("lab source must be a regular file")
    size = source.stat().st_size
    if size < 512 or size > 512 * 1024**2 or size % 512:
        raise ValueError("lab source size must be 512-byte aligned and at most 512 MiB")
    manifest = pack(source, destination)
    print(json.dumps({"cloud_disk_lab_pack": "passed", "logical_bytes": size,
                      "objects": sum("key" in item for item in manifest["chunks"])}))


def lab_serve(directory, overlay, port):
    directory, overlay = lab_path(directory), lab_path(overlay)
    if not 20000 <= port <= 60999:
        raise ValueError("lab port must be between 20000 and 60999")
    manifest = json.loads((directory / "manifest.json").read_text())
    if not 512 <= manifest.get("logical_size", 0) <= 512 * 1024**2:
        raise ValueError("lab export must be at most 512 MiB")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        listener.settimeout(120)
        print(json.dumps({"cloud_disk_lab_listener": "ready", "host": "127.0.0.1",
                          "port": port, "connections": 1}), flush=True)
        connection, _ = listener.accept()
        stats = {}
        serve_nbd_connection(connection, directory, overlay, stats=stats)
        print(json.dumps({"cloud_disk_lab_connection": stats}, sort_keys=True), flush=True)


def crash_test_serve(directory, overlay, port):
    """Single disposable NBD child that can be SIGKILLed after a FUA ACK."""
    directory, overlay = lab_path(directory), lab_path(overlay)
    if not 20000 <= port <= 60999:
        raise ValueError("crash test port out of range")
    factory = lambda: CloudDisk(directory, overlay, overlay_limit_bytes=BLOCK,
                                min_host_free_bytes=0)
    probe = factory()
    try:
        if probe.size > 32 * 1024**2:
            raise ValueError("crash test export exceeds 32 MiB")
    finally:
        probe.close()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        listener.settimeout(5)
        print(json.dumps({"crash_test_listener": "ready"}), flush=True)
        connection, _ = listener.accept()
        serve_nbd_connection(connection, directory, overlay,
                             disk_factory=factory, idle_timeout=10)


def raw_lab_serve(source, overlay, port):
    source, overlay = lab_path(source), lab_path(overlay)
    if not source.is_file() or source.stat().st_size > 512 * 1024**2:
        raise ValueError("RAW lab source must be a regular file at most 512 MiB")
    if not 20000 <= port <= 60999:
        raise ValueError("lab port must be between 20000 and 60999")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        listener.settimeout(120)
        print(json.dumps({"cloud_disk_raw_lab_listener": "ready", "host": "127.0.0.1",
                          "port": port, "connections": 1}), flush=True)
        connection, _ = listener.accept()
        stats = {}
        factory = lambda: RawDisk(source, overlay, overlay_limit_bytes=64 * 1024**2,
                                  min_host_free_bytes=512 * 1024**2)
        serve_nbd_connection(connection, None, overlay, stats=stats, disk_factory=factory)
        print(json.dumps({"cloud_disk_lab_connection": stats}, sort_keys=True), flush=True)


def r2_serve(config_path, manifest_sha256, port):
    """Loopback-only NBD backed by pinned R2 GETs and a private local overlay."""
    from cloud_disk_r2 import R2ReadOnly

    if not 20000 <= port <= 60999:
        raise ValueError("R2 NBD port must be between 20000 and 60999")
    config = validate_config(config_path)
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise ValueError("a pinned manifest SHA256 is required")
    R2ReadOnly(config, manifest_sha256)  # Credentials and account pin, no network.
    root = Path.home() / "Library/Application Support/virtualOS/cloud-disk"
    private = root / manifest_sha256
    for path in (root, private, private / "cache", private / "overlay.sqlite"):
        if path.is_symlink():
            raise ValueError("R2 private storage must not contain symlinks")
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    if private.stat().st_mode & 0o077:
        raise ValueError("R2 private storage permissions are too broad")
    boot_budget = min(BOOT_OVERLAY_RESERVE_BYTES, config["local_overlay_limit_bytes"])
    free = os.statvfs(private)
    if free.f_bavail * free.f_frsize < 10 * 1024**3 + boot_budget:
        raise OSError(errno.ENOSPC, "R2 NBD needs host reserve plus a bounded boot overlay budget")
    cache = private / "cache"
    overlay = private / "overlay.sqlite"
    # Fetch and verify the pinned manifest before reporting that NBD is ready.
    disk = open_r2_disk(config_path, manifest_sha256, overlay, cache)
    disk.close()
    factory = lambda: open_r2_disk(config_path, manifest_sha256, overlay, cache)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        print(json.dumps({"r2_nbd_listener": "ready", "host": "127.0.0.1",
                          "port": port, "account_id": config["account_id"],
                          "bucket": config["bucket"], "writes_to_r2": 0,
                          "max_connections": 8}), flush=True)
        serve_nbd_sessions(listener, None, overlay, disk_factory=factory,
                           max_connections=8, first_timeout=180,
                           reconnect_timeout=45, idle_timeout=None,
                           event_name="r2_nbd_connection")


def clone_serve(overlay):
    source = Path.home() / "VirtualOSMachines/.virtualOS-r2-staging.bundle/Disk.img"
    marker = source.parent / ".complete"
    overlay = lab_path(overlay)
    if not marker.is_file() or not source.is_file() or source.is_symlink():
        raise ValueError("isolated completed APFS clone is required")
    if not re.fullmatch(r"vos-nbd-clone-[A-Za-z0-9]+", overlay.parent.name) or overlay.name != "overlay.sqlite":
        raise ValueError("clone overlay must be in a dedicated /private/tmp/vos-nbd-clone-* directory")
    stat = os.statvfs(overlay.parent)
    if stat.f_bavail * stat.f_frsize < 11 * 1024**3:
        raise OSError(errno.ENOSPC, "clone test needs at least 11 GiB host free")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 40809))
        listener.listen(1)
        print(json.dumps({"clone_nbd_listener": "ready", "host": "127.0.0.1",
                          "port": 40809, "source": "isolated_apfs_clone",
                          "max_connections": 8, "reconnect_timeout_seconds": 45,
                          "overlay_limit_bytes": 1024**3}), flush=True)
        factory = lambda: RawDisk(source, overlay, overlay_limit_bytes=1024**3,
                                  min_host_free_bytes=10 * 1024**3)
        serve_nbd_sessions(listener, None, overlay, disk_factory=factory,
                           max_connections=8, first_timeout=180,
                           reconnect_timeout=45, idle_timeout=300,
                           event_name="clone_nbd_connection")


def bundle_serve(root, name, overlay, port, overlay_limit_bytes=2 * 1024**3,
                 min_host_free_bytes=10 * 1024**3):
    """Serve one named bundle's RAW disk from a lab root over loopback NBD.

    The production machine store is refused so this can never expose the live
    disk; the only mutable state is a bounded overlay under /private/tmp.
    """
    root = Path(root).resolve()
    production_root = (Path.home() / "VirtualOSMachines").resolve()
    if root == production_root or str(root).startswith(str(production_root) + os.sep):
        raise ValueError("refusing to serve the production machine store")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("invalid bundle name")
    disk = (root / (name + ".bundle")).resolve() / "Disk.img"
    if disk.is_symlink() or not disk.is_file():
        raise ValueError("bundle disk must be a regular file")
    production_disk = production_root / "virtualOS.bundle/Disk.img"
    if production_disk.is_file() and os.path.samefile(disk, production_disk):
        raise ValueError("refusing to serve the production RAW disk")
    overlay = lab_path(overlay)
    if overlay.name != "overlay.sqlite" or not re.fullmatch(
            r"vos-nbd-bundle-[A-Za-z0-9._-]+", overlay.parent.name):
        raise ValueError("bundle overlay must be a dedicated /private/tmp/vos-nbd-bundle-* dir")
    if not 20000 <= port <= 60999:
        raise ValueError("bundle NBD port must be between 20000 and 60999")
    stat = os.statvfs(overlay.parent)
    if stat.f_bavail * stat.f_frsize < min_host_free_bytes:
        raise OSError(errno.ENOSPC, "bundle serve needs host reserve plus overlay budget")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        print(json.dumps({"bundle_nbd_listener": "ready", "host": "127.0.0.1",
                          "port": port, "bundle": name, "source": "named_bundle",
                          "overlay_limit_bytes": overlay_limit_bytes,
                          "writes_to_source": 0}), flush=True)
        factory = lambda: RawDisk(disk, overlay, overlay_limit_bytes=overlay_limit_bytes,
                                  min_host_free_bytes=min_host_free_bytes)
        serve_nbd_sessions(listener, None, overlay, disk_factory=factory,
                           max_connections=8, first_timeout=180,
                           reconnect_timeout=45, idle_timeout=None,
                           event_name="bundle_nbd_connection")


if __name__ == "__main__":
    if sys.argv[1:] == ["self-test"]:
        self_test()
    elif len(sys.argv) == 3 and sys.argv[1] == "check-config":
        try:
            config = validate_config(sys.argv[2])
        except ValueError as error:
            raise SystemExit(f"cloud_disk_config=pending_or_invalid: {error}") from None
        print(json.dumps({"cloud_disk_config": "valid", "account_id": config["account_id"],
                          "bucket": config["bucket"], "credential_material": "none"}))
    elif len(sys.argv) == 4 and sys.argv[1] == "lab-pack":
        lab_pack(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 5 and sys.argv[1] == "lab-serve":
        lab_serve(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif len(sys.argv) == 5 and sys.argv[1] == "crash-test-serve":
        crash_test_serve(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif len(sys.argv) == 5 and sys.argv[1] == "raw-lab-serve":
        raw_lab_serve(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif len(sys.argv) == 3 and sys.argv[1] == "clone-serve":
        clone_serve(sys.argv[2])
    elif len(sys.argv) == 6 and sys.argv[1] == "bundle-serve":
        bundle_serve(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    elif len(sys.argv) == 5 and sys.argv[1] == "r2-serve":
        r2_serve(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif len(sys.argv) == 3 and sys.argv[1] == "r2-upload-clone":
        from cloud_disk_r2_upload import upload_staging_clone
        print(json.dumps({"r2_clone_upload": upload_staging_clone(sys.argv[2])},
                         sort_keys=True), flush=True)
    else:
        raise SystemExit("usage: cloud_disk.py self-test|check-config PATH|lab-pack SOURCE DEST|lab-serve ROOT OVERLAY PORT|bundle-serve ROOT NAME OVERLAY PORT|r2-serve CONFIG MANIFEST_SHA256 PORT (use computer-space.sh)")
