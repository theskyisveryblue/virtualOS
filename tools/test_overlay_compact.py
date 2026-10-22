import fcntl
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from cloud_disk import BLOCK, RawDisk
from overlay_compact import compact_copy


class OfflineCompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vos-offline-compact-', dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'base.raw'; self.base.write_bytes(bytes(16 * BLOCK))
        self.source = self.root / 'overlay.sqlite'
        self.destination = self.root / 'compacted.sqlite'
        disk = RawDisk(self.base, self.source, overlay_limit_bytes=16 * BLOCK, min_host_free_bytes=0)
        disk.write(0, b'x' * (16 * BLOCK))
        disk.write(BLOCK, bytes(15 * BLOCK))
        disk.close()
        self.source.chmod(0o600)
        self.before = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def compact(self, **kwargs):
        return compact_copy(self.source, self.destination, probe=lambda path: True, reserve=0, **kwargs)

    def test_compact_copy_is_smaller_preserves_source_and_guest_reads(self):
        row = self.compact()
        self.assertGreater(row['candidate_bytes_saved'], 10 * BLOCK)
        self.assertEqual(row['blocks'], 1)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)
        disk = RawDisk(self.base, self.destination, min_host_free_bytes=0)
        try:
            self.assertEqual(disk.read(0, 16 * BLOCK), b'x' * BLOCK + bytes(15 * BLOCK))
        finally:
            disk.close()

    def test_running_server_lock_is_refused(self):
        with self.source.with_suffix('.lock').open('w') as lock:
            self.source.with_suffix('.lock').chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): self.compact()
        self.assertFalse(self.destination.exists())

    def test_open_file_or_pending_journal_is_refused(self):
        with self.assertRaises(ValueError):
            compact_copy(self.source, self.destination, probe=lambda path: False, reserve=0)
        wal = Path(str(self.source) + '-wal'); wal.write_bytes(b'pending'); wal.chmod(0o600)
        with self.assertRaises(ValueError): self.compact()
        self.assertFalse(self.destination.exists())

    def test_existing_destination_is_preserved(self):
        self.destination.write_bytes(b'keep')
        with self.assertRaises(ValueError): self.compact()
        self.assertEqual(self.destination.read_bytes(), b'keep')

    def test_unused_shm_in_private_parent_is_allowed_but_shared_writes_are_not(self):
        shm = Path(str(self.source) + '-shm'); shm.write_bytes(bytes(32768)); shm.chmod(0o644)
        self.assertTrue(self.compact()['logical_contents_verified'])
        self.destination.unlink()
        shm.chmod(0o666)
        with self.assertRaises(ValueError): self.compact()
        self.assertFalse(self.destination.exists())

    def test_linked_or_shared_source_is_refused(self):
        saved = self.root / 'saved'; self.source.rename(saved); self.source.symlink_to(saved)
        with self.assertRaises(ValueError): self.compact()
        self.source.unlink(); os.link(saved, self.source)
        with self.assertRaises(ValueError): self.compact()
        self.assertFalse(self.destination.exists())

    def test_changed_source_and_corrupt_schema_never_publish(self):
        def changed(path):
            self.source.touch()
            return True
        with self.assertRaises(ValueError):
            compact_copy(self.source, self.destination, probe=changed, reserve=0)
        with closing(sqlite3.connect(self.source)) as database:
            with database:
                database.execute('CREATE TABLE unexpected (value TEXT)')
        with self.assertRaises(ValueError): self.compact()
        self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
