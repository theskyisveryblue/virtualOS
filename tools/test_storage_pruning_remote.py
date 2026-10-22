from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import storage_pruning_remote as remote


class RemotePruningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.private = self.root / '.local/maintenance/compressed-writes-20261021'
        self.private.mkdir(parents=True, mode=0o700)
        self.source = self.root / '.virtualos-nbd/vm-a/overlay.sqlite'
        self.source.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE TABLE blocks (idx INTEGER PRIMARY KEY, data BLOB)')
            db.execute('CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)')
            db.execute('INSERT INTO blocks VALUES (0,?)', (b'x' * 65536,))
        (self.root / 'shared-disks').mkdir()
        (self.root / 'shared-disks/vm-a.json').write_text(json.dumps(dict(
            baseName='vm-a-base-20261021', delta=str(self.root / 'deltas/vm-a-compressed-20261021.sqlite'),
            deltaFormat='compressed-v1',
            deltaSHA256='ed2a522a2ff69e3210927c83df8784478794474d849371215168f8bd8332ba6b')))
        original = self.root / 'vm-a-original-20261021.bundle/Disk.img'
        original.parent.mkdir()
        original.write_bytes(b'original')
        self.before = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def execute(self, load):
        def path(value):
            return self.root if value == '/Users/ai/vos-fleet' else Path(value)
        def output(command, **kwargs):
            if command[0] == 'bash':
                return json.dumps(dict(memory_free_percent=60, swap_used_mib=1024))
            return '440 production\n2499 linux\n80901 test\n'
        def audit(base, seed, pin, snapshot, **kwargs):
            with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                self.assertEqual(db.execute('SELECT data FROM blocks').fetchone()[0], b'x' * 65536)
            return dict(read_only=True, closed_snapshot=True, blocks=1, redundant_blocks=0,
                        actual_reclaimed_bytes=0)
        with patch.object(remote, 'Path', side_effect=path), \
             patch.object(remote.os, 'statvfs', return_value=SimpleNamespace(f_bavail=100 * 1024**3, f_frsize=1)), \
             patch.object(remote.os, 'getloadavg', side_effect=load), \
             patch.object(remote.os, 'cpu_count', return_value=10), \
             patch.object(remote.subprocess, 'check_output', side_effect=output), \
             patch('overlay_pruning_audit.audit', side_effect=audit), \
             patch.object(remote.sys, 'path', list(remote.sys.path)), \
             patch.object(remote.sys, 'dont_write_bytecode', True):
            result = remote.run({})
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.before)
        self.assertEqual(list(self.private.iterdir()), [])
        return result

    def test_pressure_refuses_before_snapshot(self):
        self.assertEqual(self.execute(lambda: (20, 0, 0))['state'], 'waiting_for_resources')

    def test_consistent_snapshot_is_closed_and_released_after_audit(self):
        result = self.execute(lambda: (0, 0, 0))
        self.assertEqual(result['state'], 'verified')
        self.assertTrue(result['temporary_snapshot_released'])

    def test_pressure_during_backup_releases_partial_snapshot(self):
        calls = iter([(0, 0, 0), (20, 0, 0)])
        self.assertEqual(self.execute(lambda: next(calls))['state'], 'waiting_for_resources')


if __name__ == '__main__':
    unittest.main()
