import json
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from linux_lab_resources import policy, private_output_parent, watch


class ResourceTests(unittest.TestCase):
    report = dict(disk_free_kib=20 * 1024**2, swap_used_mib=12 * 1024,
                  memory_free_percent=45, load_one=5, cpu_count=10)

    def test_one_gib_guest_requires_fresh_memory_and_cpu_headroom(self):
        self.assertEqual(policy(self.report)['linux_lab'], 'allowed')
        for changes in ({'memory_free_percent': 39}, {'swap_used_mib': 16 * 1024},
                        {'disk_free_kib': 11 * 1024**2}, {'load_one': 10},
                        {'memory_free_percent': None}, {'load_one': float('nan')}):
            with self.subTest(changes=changes):
                self.assertEqual(policy(dict(self.report, **changes))['linux_lab'], 'paused')

    def test_watch_signals_abort_on_pressure_without_signalling_any_process(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            flag = Path(name) / 'abort'
            report = Mock(side_effect=[self.report, dict(self.report, memory_free_percent=14)])
            watch(123, flag, report=report, pause=lambda _: None, alive=lambda: None)
            self.assertIn('memory_headroom', json.loads(flag.read_text())['reasons'])
            self.assertEqual(report.call_count, 2)

    def test_unknown_measurements_and_runtime_expiry_abort(self):
        for report, deadline, reason in ((lambda: {}, None, 'resource_measurement_unavailable'),
                (lambda: self.report, time.monotonic() - 1, 'guest_runtime_limit')):
            with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
                flag = Path(name) / 'abort'
                watch(123, flag, report=report, pause=lambda _: None,
                      alive=lambda: None, deadline=deadline)
                self.assertIn(reason, json.loads(flag.read_text())['reasons'])

    def test_completed_guest_does_not_leave_an_abort_flag(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            flag = Path(name) / 'abort'
            watch(123, flag, alive=Mock(side_effect=ProcessLookupError()))
            self.assertFalse(flag.exists())

    def test_persistent_output_requires_a_private_real_directory(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            root = Path(name)
            path = root / 'runs'
            private_output_parent(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            alias = root / 'alias'; alias.symlink_to(path)
            with self.assertRaises(ValueError):
                private_output_parent(alias)
            refused = subprocess.run([sys.executable, str(Path(__file__).with_name('linux_lab_resources.py')),
                'prepare-output', str(alias)], capture_output=True, text=True)
            self.assertEqual(refused.returncode, 2)
            path.chmod(0o755)
            with self.assertRaises(ValueError):
                private_output_parent(path)
