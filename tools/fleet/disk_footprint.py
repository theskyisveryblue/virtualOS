"""Measure exact base-relative storage from immutable scratch disk copies."""
import hashlib
import os
from pathlib import Path
import stat
import time
import zlib

BLOCK = 65536


def measure(base, machine, *, reserve=10 * 1024**3, maximum_seconds=180):
    paths = [Path(base), Path(machine)]
    production = Path.home() / 'VirtualOSMachines'
    for path in paths:
        if path.is_symlink() or production.resolve() in path.resolve().parents:
            raise ValueError('measure immutable scratch copies, never the production store')
    started = time.monotonic()
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    descriptors = []
    try:
        for path in paths:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            descriptors.append(descriptor)
        before = [os.fstat(fd) for fd in descriptors]
        if any(not stat.S_ISREG(row.st_mode) or row.st_size <= 0 or row.st_size % 512 for row in before):
            raise ValueError('aligned regular disk copies required')
        if before[0].st_size != before[1].st_size:
            raise ValueError('comparison requires equal logical disk sizes')
        hashes = [hashlib.sha256(), hashlib.sha256()]
        changed = raw_delta = encoded_delta = zero_changes = base_nonzero = machine_nonzero = 0
        size = before[0].st_size
        for offset in range(0, size, BLOCK):
            if offset % (128 * 1024**2) == 0:
                free = os.statvfs(paths[0].parent)
                if free.f_bavail * free.f_frsize < reserve or time.monotonic() - started > maximum_seconds:
                    raise OSError('measurement stopped to preserve resource budget')
            length = min(BLOCK, size-offset)
            first, second = [os.pread(fd, length, offset) for fd in descriptors]
            if len(first) != length or len(second) != length:
                raise ValueError('disk copy truncated during measurement')
            hashes[0].update(first); hashes[1].update(second)
            base_nonzero += first.count(0) != len(first)
            machine_nonzero += second.count(0) != len(second)
            if first != second:
                changed += 1
                raw_delta += length
                encoded_delta += len(zlib.compress(second, level=1))
                zero_changes += second.count(0) == len(second)
        if any(identity(os.fstat(fd)) != identity(info) for fd, info in zip(descriptors, before)):
            raise ValueError('disk copy changed during measurement')
        return dict(logical_bytes_per_disk=size, block_size=BLOCK,
                    base_sha256=hashes[0].hexdigest(), machine_sha256=hashes[1].hexdigest(),
                    differing_blocks=changed, delta_raw_bytes=raw_delta,
                    delta_zlib_level1_payload_bytes=encoded_delta,
                    delta_payload_excludes_manifest_and_database_overhead=True,
                    changed_to_zero_blocks=zero_changes,
                    base_nonzero_blocks=base_nonzero, machine_nonzero_blocks=machine_nonzero,
                    bytes_avoided_vs_independent_full_logical_disk=size-raw_delta,
                    actual_unique_apfs_extents_measured=False,
                    guest_consistent_migration_snapshot_verified=False,
                    elapsed_seconds=time.monotonic()-started)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
