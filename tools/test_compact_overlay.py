"""Prove overlay compaction preserves guest bytes, limits and offline writes."""
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cloud_disk import BLOCK, CHUNK, CloudDisk, RawDisk, pack


class CompactOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.raw'
        self.original = b'a' * BLOCK + b'b' * BLOCK + b'c' * 512
        self.source.write_bytes(self.original)
        self.overlay = self.root / 'overlay.sqlite'

    def raw(self, limit=BLOCK):
        disk = RawDisk(self.source, self.overlay, overlay_limit_bytes=limit, min_host_free_bytes=0)
        self.addCleanup(disk.close)
        return disk

    def count(self, disk):
        return disk.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]

    def test_restore_to_base_removes_block_and_survives_restart(self):
        disk = self.raw()
        disk.write(11, b'changed')
        self.assertEqual(self.count(disk), 1)
        disk.write(11, self.original[11:18])
        self.assertEqual(self.count(disk), 0)
        disk.flush()
        # A second connection proves committed state without closing twice.
        reopened = self.raw()
        self.assertEqual(reopened.read(0, len(self.original)), self.original)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_noop_base_writes_need_no_overlay_capacity_or_space(self):
        disk = self.raw(limit=0)
        with patch.object(disk, '_ensure_host_space', side_effect=AssertionError('no write reserve needed')):
            disk.write(0, self.original)
        self.assertEqual(self.count(disk), 0)
        self.assertEqual(disk.read(0, len(self.original)), self.original)

    def test_net_capacity_allows_removing_one_block_and_adding_another(self):
        disk = self.raw()
        disk.write(0, b'x' * BLOCK)
        disk.write(0, self.original[:BLOCK] + b'y' * BLOCK)
        self.assertEqual(self.count(disk), 1)
        self.assertEqual(disk.read(0, 2 * BLOCK), b'a' * BLOCK + b'y' * BLOCK)

    def test_capacity_and_host_space_failures_preserve_previous_changes(self):
        disk = self.raw()
        disk.write(0, b'x' * BLOCK)
        before = disk.read(0, len(self.original))
        with self.assertRaises(OSError) as stopped:
            disk.write(0, b'y' * (2 * BLOCK))
        self.assertEqual(stopped.exception.errno, errno.ENOSPC)
        self.assertEqual(disk.read(0, len(self.original)), before)
        with patch.object(disk, '_ensure_host_space', side_effect=OSError(errno.ENOSPC, 'full')):
            with self.assertRaises(OSError):
                disk.write(0, self.original[:BLOCK] + b'y' * BLOCK)
        self.assertEqual(disk.read(0, len(self.original)), before)

    def test_zero_over_nonzero_base_stays_a_real_difference(self):
        disk = self.raw()
        disk.write(0, bytes(BLOCK))
        self.assertEqual(self.count(disk), 1)
        self.assertEqual(disk.read(0, BLOCK), bytes(BLOCK))
        disk.write(0, b'a' * BLOCK)
        self.assertEqual(self.count(disk), 0)

    def test_cached_cloud_base_can_remove_restored_blocks(self):
        base = self.root / 'base'; pack(self.source, base)
        disk = CloudDisk(base, self.overlay, min_host_free_bytes=0)
        self.addCleanup(disk.close)
        disk.read(0, 1)
        disk.write(0, b'x' * BLOCK)
        disk.write(0, b'a' * BLOCK)
        self.assertEqual(self.count(disk), 0)
        self.assertEqual(disk.read(0, len(self.original)), self.original)

    def test_full_cloud_write_stays_offline_and_zero_manifest_needs_no_rows(self):
        base = self.root / 'base'; pack(self.source, base)
        with patch.object(CloudDisk, '_encoded', side_effect=AssertionError('no base fetch allowed')):
            disk = CloudDisk(base, self.overlay, min_host_free_bytes=0)
            self.addCleanup(disk.close)
            disk.write(0, b'a' * BLOCK)
            self.assertEqual(self.count(disk), 1)  # Unknown base must retain the write.
            self.assertEqual(disk.read(0, BLOCK), b'a' * BLOCK)
        zero_source = self.root / 'zero.raw'; zero_source.write_bytes(bytes(CHUNK))
        zero_base = self.root / 'zero-base'; pack(zero_source, zero_base)
        zero = CloudDisk(zero_base, self.root / 'zero.sqlite', overlay_limit_bytes=0, min_host_free_bytes=0)
        self.addCleanup(zero.close)
        with patch.object(zero, '_encoded', side_effect=AssertionError('zero base needs no fetch')):
            zero.write(0, bytes(BLOCK))
        self.assertEqual(self.count(zero), 0)


if __name__ == '__main__':
    unittest.main()
