"""Rollback must preserve writes made after switching to compressed storage."""
import hashlib
import unittest

from cloud_disk import BLOCK
from compressed_delta import CompressedDeltaDisk
from compressed_write_overlay import CompressedWriteDisk, convert_copy
import test_compressed_write_overlay as fixtures


class RollbackTests(unittest.TestCase):
    def test_reverse_conversion_preserves_post_migration_writes(self):
        fixture = fixtures.CompressedWriteTests('test_conversion_and_reverse_keep_source_and_logical_fingerprint')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before_migration = fixture.raw_overlay()
        original_hash = hashlib.sha256(before_migration.read_bytes()).hexdigest()
        compressed = fixture.root / 'new-writes.sqlite'
        initial = convert_copy(fixture.base, fixture.delta, fixture.pin, before_migration,
                               compressed, reserve=0, probe=lambda path: True)
        disk = CompressedWriteDisk(fixture.base, fixture.delta, compressed, fixture.pin,
                                   min_host_free_bytes=0)
        try:
            disk.write(2 * BLOCK, b'y' * BLOCK)
            disk.flush()
        finally:
            disk.close()
        current_hash = hashlib.sha256(compressed.read_bytes()).hexdigest()
        rollback = fixture.root / 'rollback.sqlite'
        result = convert_copy(fixture.base, fixture.delta, fixture.pin, compressed,
                              rollback, to_compressed=False, reserve=0, probe=lambda path: True)
        self.assertNotEqual(initial['logical_fingerprint'], result['logical_fingerprint'])
        self.assertEqual(hashlib.sha256(compressed.read_bytes()).hexdigest(), current_hash)
        self.assertEqual(hashlib.sha256(before_migration.read_bytes()).hexdigest(), original_hash)
        raw = CompressedDeltaDisk(fixture.base, fixture.delta, rollback, fixture.pin,
                                  min_host_free_bytes=0)
        try:
            self.assertEqual(raw.read(0, raw.size), b'x' * (2 * BLOCK) + b'y' * BLOCK + b'c' * 512)
        finally:
            raw.close()


if __name__ == '__main__':
    unittest.main()
