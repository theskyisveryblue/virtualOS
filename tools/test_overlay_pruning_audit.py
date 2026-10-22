import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from cloud_disk import BLOCK
from compressed_delta import CompressedDeltaDisk, build
from overlay_pruning_audit import audit


class PruningAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / 'base.raw'
        self.machine = self.root / 'machine.raw'
        self.seed = self.root / 'seed.sqlite'
        self.snapshot = self.root / 'snapshot.sqlite'
        self.base.write_bytes(b'a' * BLOCK + b'b' * BLOCK + b'c' * 512)
        self.machine.write_bytes(b'a' * BLOCK + b'd' * BLOCK + b'e' * 512)
        self.pin = build(self.base, self.machine, self.seed, reserve=0)['delta_sha256']
        disk = CompressedDeltaDisk(self.base, self.seed, self.snapshot, self.pin, min_host_free_bytes=0)
        disk.close()
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            # Two redundant rows, one raw-base-equal row that differs from seed.
            db.executemany('INSERT INTO blocks VALUES (?,?)',
                           [(0, b'a' * BLOCK), (1, b'b' * BLOCK), (2, b'e' * 512)])

    def hashes(self):
        return [hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.base, self.seed, self.snapshot)]

    def run_audit(self, **kw):
        return audit(self.base, self.seed, self.pin, self.snapshot, probe=lambda _: True, **kw)

    def test_counts_against_seed_and_base_and_keeps_all_bytes(self):
        before = self.hashes()
        result = self.run_audit()
        self.assertEqual(result['blocks'], 3)
        self.assertEqual(result['redundant_blocks'], 2)
        self.assertEqual(result['redundant_payload_bytes'], BLOCK + 512)
        self.assertEqual(result['actual_reclaimed_bytes'], 0)
        self.assertEqual(self.hashes(), before)

    def test_open_source_and_pending_journal_refused(self):
        with self.assertRaisesRegex(ValueError, 'closed'):
            audit(self.base, self.seed, self.pin, self.snapshot, probe=lambda _: False)
        journal = Path(str(self.snapshot) + '-journal')
        journal.write_bytes(b'pending')
        with self.assertRaisesRegex(ValueError, 'journals'):
            self.run_audit()

    def test_wrong_seed_or_snapshot_identity_refused(self):
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            audit(self.base, self.seed, '0' * 64, self.snapshot, probe=lambda _: True)
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            db.execute("UPDATE metadata SET value='wrong'")
        with self.assertRaisesRegex(ValueError, 'backing'):
            self.run_audit()

    def test_guard_interrupt_preserves_inputs_and_releases_lock(self):
        before = self.hashes()
        calls = []
        def guard():
            calls.append(1)
            if len(calls) == 2:
                raise OSError('pressure')
        with self.assertRaisesRegex(OSError, 'pressure'):
            self.run_audit(guard=guard)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(self.run_audit()['blocks'], 3)

    def test_out_of_range_block_is_rejected_without_changes(self):
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            db.execute('INSERT INTO blocks VALUES (?,?)', (99, b'x' * BLOCK))
        before = self.hashes()
        with self.assertRaisesRegex(ValueError, 'invalid snapshot block'):
            self.run_audit()
        self.assertEqual(self.hashes(), before)


if __name__ == '__main__':
    unittest.main()
