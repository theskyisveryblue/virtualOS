"""Exercise the canonical observer's embedded programs without host access."""
import contextlib
import ast
import io
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).with_name('cloud-disk-observe.sh').read_text()
PROGRAMS = [part.split('\nPY\n', 1)[0] for part in re.split(r"<<'PY'[^\n]*\n", SOURCE)[1:]]


class ObserverTests(unittest.TestCase):
    def test_storage_observation_reads_raw_and_encoded_tables_without_writes(self):
        tree = ast.parse(PROGRAMS[1])
        remote = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'remote' for target in node.targets))
        source = remote.split('# storage-snapshot:start\n', 1)[1].split('# storage-snapshot:end', 1)[0]
        for encoded in (False, True):
            with self.subTest(encoded=encoded), tempfile.TemporaryDirectory() as name:
                root = Path(name).resolve()
                path = root / 'vos-fleet/.virtualos-nbd/vm-a/overlay.sqlite'
                path.parent.mkdir(parents=True)
                table = 'encoded_blocks' if encoded else 'blocks'
                with sqlite3.connect(path) as db:
                    db.execute('CREATE TABLE ' + table + '(idx INTEGER PRIMARY KEY,data BLOB)')
                    db.execute('INSERT INTO ' + table + ' VALUES (0,?)', (b'fixture',))
                    if encoded:
                        # No decoder is installed: monitoring must avoid the decoded view.
                        db.execute('CREATE VIEW blocks AS SELECT idx,missing_decoder(data) AS data FROM encoded_blocks')
                original = path.read_bytes()
                output = io.StringIO()
                with patch('pathlib.Path.home', return_value=root), contextlib.redirect_stdout(output):
                    exec(source, dict(pathlib=__import__('pathlib'), os=os, stat=stat, json=json))
                report = json.loads(output.getvalue().split('=', 1)[1])
                self.assertEqual(report['codec'], 'zlib-v1' if encoded else 'raw')
                self.assertEqual(report['changed_blocks'], 1)
                self.assertEqual(report['logical_changed_bytes'], 65536)
                self.assertTrue(report['read_only'])
                self.assertIsInstance(report['volume_available_bytes'], int)
                self.assertGreaterEqual(report['volume_available_bytes'], 0)
                self.assertEqual(path.read_bytes(), original)

    def test_probe_failures_preserve_later_evidence_and_relay_check(self):
        for failure in (PermissionError('private diagnostic'),
                        subprocess.CalledProcessError(1, ['ps']),
                        subprocess.TimeoutExpired(['ps'], 5)):
            with self.subTest(error=type(failure).__name__), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                state = root / 'state'; state.mkdir()
                (state / 'result.json').write_text(json.dumps(dict(
                    readback_verified=True, logical_bytes=32212254720)))
                connections = types.ModuleType('cloud_disk_connections')
                connections.control_options = Mock(return_value=[])
                connections.relay_status = Mock(return_value={'isStale': False})
                loop = types.ModuleType('cloud_disk_work_loop')
                loop.control_observation = Mock(return_value={'completed': ['archive_conversion']})
                budget = types.ModuleType('ocr_budget')
                budget.resources = Mock(side_effect=PermissionError('private diagnostic'))
                selection = types.ModuleType('linux_guest_selection')
                selection.read_json = Mock(return_value={})
                modules = dict(cloud_disk_connections=connections, cloud_disk_work_loop=loop,
                               ocr_budget=budget, linux_guest_selection=selection)
                program = PROGRAMS[1].replace('/private/tmp/virtualos-archive-conversion', str(state))
                output = io.StringIO()
                def run(command, **kwargs):
                    if command[0] == 'ps':
                        self.assertTrue(kwargs['check'])
                        self.assertEqual(kwargs['timeout'], 5)
                        raise failure
                    self.assertEqual(command[0], 'ssh')
                    raise PermissionError('private diagnostic')
                with patch.dict(sys.modules, modules), patch.object(sys, 'argv', ['observer', str(root)]), \
                     patch('subprocess.run', side_effect=run), contextlib.redirect_stdout(output):
                    with self.assertRaises(SystemExit) as stopped:
                        exec(compile(program, 'observer-snapshot', 'exec'), {})
                    self.assertEqual(stopped.exception.code, 1)
                report = output.getvalue()
                self.assertIn('operator_resource_snapshot=PermissionError', report)
                self.assertIn('"available": false', report)
                self.assertIn('result.json={"logical_bytes": 32212254720, "readback_verified": true}', report)
                self.assertIn('host_security_last_review=', report)
                self.assertIn('relay_direct={"isStale": false}', report)
                self.assertIn('mini_access=PermissionError', report)
                self.assertNotIn('private diagnostic', report)
                self.assertNotIn('Traceback', report)
                connections.relay_status.assert_called_once_with()

    def test_runner_lock_prevents_duplicates_without_process_inspection(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            tools = root / 'tools'; tools.mkdir()
            # Replace the long-running observer with a single disposable process.
            (tools / 'cloud-disk-observe.sh').write_text(
                'exec python3 -c "import time; time.sleep(30)"\n')
            state = root / 'state'
            program = PROGRAMS[0].replace('/private/tmp/virtualos-archive-conversion', str(state))
            children = []
            spawn = subprocess.Popen
            def start(*args, **kwargs):
                child = spawn(*args, **kwargs)
                children.append(child)
                return child
            try:
                with patch.object(sys, 'argv', ['observer', str(root)]), \
                     patch('subprocess.run', side_effect=PermissionError('ps unavailable')), \
                     patch('subprocess.Popen', side_effect=start):
                    reports = []
                    for _ in range(2):
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            exec(compile(program, 'observer-launcher', 'exec'), {})
                        reports.append(json.loads(output.getvalue().splitlines()[0]))
                        snapshot = state / 'observer-latest.txt'
                        snapshot.write_text('observed_at=fixture-cache\n')
                        os.utime(snapshot, (1, 1))
                    self.assertEqual(len(children), 1)
                    self.assertEqual(reports[0]['pid'], reports[1]['pid'])
                    self.assertIsNone(reports[0]['snapshot_age_seconds'])
                    self.assertGreater(reports[1]['snapshot_age_seconds'], 120)
                    children[0].terminate(); children[0].wait(timeout=5)
                    with contextlib.redirect_stdout(io.StringIO()):
                        exec(compile(program, 'observer-launcher', 'exec'), {})
                    self.assertEqual(len(children), 2)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
