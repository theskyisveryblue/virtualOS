"""Measure reclaimed SQLite pages using disposable data, never a live disk."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_disk import BLOCK, RawDisk


def measure():
    with tempfile.TemporaryDirectory(prefix='vos-compact-overlay-', dir='/private/tmp') as directory:
        root = Path(directory)
        source = root / 'base.raw'
        size = 128 * BLOCK
        with source.open('wb') as stream:
            stream.truncate(size)
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        overlay = root / 'overlay.sqlite'
        disk = RawDisk(source, overlay, overlay_limit_bytes=size, min_host_free_bytes=0)
        try:
            disk.write(0, b'x' * size)
            disk.flush()
            before = overlay.stat().st_size
            rows_before = disk.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]
            disk.write(0, bytes(size))
            disk.flush()
            rows_after = disk.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]
            reusable = disk.db.execute('PRAGMA freelist_count').fetchone()[0]
            assert disk.read(0, size) == bytes(size)
        finally:
            disk.close()
        # This is the disposable, now-closed fixture only. Live overlays require
        # coordinated maintenance; no generic live-file VACUUM is exposed here.
        with sqlite3.connect(overlay) as database:
            database.execute('PRAGMA journal_mode=DELETE')
            database.execute('VACUUM')
        after = overlay.stat().st_size
        reopened = RawDisk(source, overlay, overlay_limit_bytes=size, min_host_free_bytes=0)
        try:
            assert reopened.read(0, size) == bytes(size)
        finally:
            reopened.close()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        assert rows_before == 128 and rows_after == 0 and after < before
        return dict(scope='disposable_synthetic_fixture', changed_blocks_before=rows_before,
                    changed_blocks_after=rows_after, reusable_sqlite_pages=reusable,
                    database_bytes_before=before, database_bytes_after_offline_vacuum=after,
                    database_bytes_reclaimed=before-after, restart_readback_verified=True,
                    immutable_base_unchanged=True, running_disks_modified=False)


if __name__ == '__main__':
    print(json.dumps(measure(), sort_keys=True))
