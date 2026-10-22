import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from mac_mini.reclaim_idle_artifacts import reclaim


class ReclaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profile'
        self.path.mkdir()
        (self.path / 'cache').write_text('disposable')
        old = time.time() - 48 * 3600
        os.utime(self.path, (old, old))

    def probe(self, command, **kwargs):
        if command[0] == 'ps':
            return subprocess.CompletedProcess(command, 0, '', '')
        if command[0] == 'lsof':
            return subprocess.CompletedProcess(command, 1, '', '')
        return subprocess.CompletedProcess(command, 0, '4\tfixture\n', '')

    def test_dry_run_and_apply_preserve_symlink_destinations(self):
        outside = Path(self.temp.name) / 'outside'; outside.mkdir()
        (outside / 'keep').write_text('retain')
        (self.path / 'linked').symlink_to(outside, target_is_directory=True)
        old = time.time() - 48 * 3600
        os.utime(self.path, (old, old))
        with patch('subprocess.run', side_effect=self.probe):
            self.assertTrue(reclaim(self.path)['eligible'])
            self.assertTrue(self.path.exists())
            self.assertTrue(reclaim(self.path, apply=True)['removed'])
        self.assertTrue((outside / 'keep').exists())

    def test_recent_or_linked_root_is_refused(self):
        os.utime(self.path, None)
        with patch('subprocess.run', side_effect=AssertionError('no probes for unsafe roots')):
            self.assertFalse(reclaim(self.path, apply=True)['eligible'])
            link = Path(self.temp.name) / 'link'; link.symlink_to(self.path)
            self.assertFalse(reclaim(link, apply=True)['eligible'])
        self.assertTrue(self.path.exists())

    def test_busy_and_failed_probes_never_remove_target(self):
        for result in (subprocess.CompletedProcess([], 0, '123\n', ''),
                       subprocess.CompletedProcess([], 1, '123\n', ''),
                       subprocess.CompletedProcess([], 1, '', 'uncertain'),
                       subprocess.TimeoutExpired(['ps'], 5)):
            with self.subTest(result=result), patch('subprocess.run') as run:
                if isinstance(result, Exception):
                    run.side_effect = result
                else:
                    run.side_effect = [self.probe(['ps']), result]
                self.assertFalse(reclaim(self.path, apply=True)['eligible'])
                self.assertTrue(self.path.exists())


if __name__ == '__main__':
    unittest.main()
