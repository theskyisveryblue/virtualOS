import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cloud_disk_work_loop import archive_action, available, step, control_tasks, control_tick
from agent_control import Control


class SupervisorTests(unittest.TestCase):
    def test_known_pruning_job_dispatches_only_when_eligible(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'result.json').write_text('{}')
            (directory / 'linux-work.json').write_text('{"state":"verified"}')
            launches = []
            with patch('storage_pruning_job.eligible', return_value=True), \
                 patch('cloud_disk_work_loop.recovery_admission', return_value={'allowed': False}):
                step(directory, launches.append)
            self.assertEqual(len(launches), 1)
            self.assertTrue(launches[0][1].endswith('storage_pruning_job.py'))
            self.assertEqual(launches[0][2], 'start')

    def test_storage_agenda_is_durable_and_background_does_not_renew_worker(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name).resolve()
            control = Control(directory / 'agent-control', Path(__file__).resolve().parents[1])
            control.seed([dict(id='fixture_active_work', title='Independent active fixture')])
            control.seed(control_tasks())
            lease = control.claim('codex-worker', ttl=300)
            before = control.snapshot()
            with patch.object(Control, 'heartbeat', side_effect=AssertionError('unattended heartbeat')), \
                 patch.object(Control, 'claim', side_effect=AssertionError('unattended claim')):
                control_tick(directory)
            after = control.snapshot()
            self.assertGreaterEqual(len(after['tasks']), 40)
            self.assertEqual(after['leases']['worker']['expires_at'], lease['expires_at'])
            self.assertEqual(len([row for row in after['tasks'] if row['status'] == 'active']), 1)
            self.assertEqual(len(before['tasks']), len(after['tasks']))
            for name in ('recovery_boot', 'host_hardening', 'native_supervisor_activation'):
                row = next(row for row in after['tasks'] if row['id'] == name)
                self.assertIn('approval', row['block_reasons'])

    def test_running_or_completed_archive_is_preserved(self):
        self.assertEqual(archive_action({}, False, False), 'observe')
        self.assertEqual(archive_action({}, True, True), 'observe')

    def test_only_classified_connection_failure_is_restarted(self):
        self.assertEqual(archive_action({'state': 'failed', 'retryable_connection_failure': True},
                                        True, False), 'restart')
        self.assertEqual(archive_action({'state': 'failed', 'error': 'OSError'}, True, False), 'restart')
        self.assertEqual(archive_action({'state': 'failed', 'error': 'PermissionError'}, True, False), 'requires_attention')
        for state in ({'state': 'failed'}, {'state': 'failed', 'remote_worker_failed': True,
                                          'retryable_connection_failure': True}):
            self.assertEqual(archive_action(state, True, False), 'requires_attention')

    def test_missing_controller_can_reconnect_without_new_generation(self):
        self.assertEqual(archive_action({'state': 'converting'}, True, False), 'restart')

    def test_integrity_failure_does_not_block_independent_linux_continuation(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'status.json').write_text(json.dumps({'state': 'failed', 'remote_worker_failed': True}))
            launches = []
            row = step(directory, launches.append)
            self.assertEqual(row['archive_action'], 'requires_attention')
            self.assertEqual(len(launches), 1)
            self.assertTrue(launches[0][1].endswith('linux_lab_work.py'))

    def test_verified_linux_and_finished_recovery_do_not_repeat(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            for filename, row in [('status.json', {'state': 'complete'}),
                                  ('linux-work.json', {'state': 'verified'}),
                                  ('recovery-watch.json', {'state': 'recovery_finished'})]:
                (directory / filename).write_text(json.dumps(row))
            (directory / 'result.json').write_text('{}')
            launches = []
            step(directory, launches.append)
            self.assertEqual(launches, [])

    def test_locked_controller_is_observed(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'lock'
            with path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertFalse(available(path))
            self.assertTrue(available(path))

    def test_blocked_recovery_is_never_dispatched(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'result.json').write_text('{}')
            (directory / 'linux-work.json').write_text('{"state":"verified"}')
            launches = []
            with patch('cloud_disk_work_loop.recovery_admission', return_value={'allowed': False}):
                step(directory, launches.append)
            self.assertEqual(launches, [])

    def test_eligible_recovery_can_restart_its_watcher(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'result.json').write_text('{}')
            (directory / 'linux-work.json').write_text('{"state":"verified"}')
            launches = []
            with patch('cloud_disk_work_loop.recovery_admission', return_value={'allowed': True}):
                step(directory, launches.append)
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0][-1], 'watch-start')

    def test_shell_entrypoints_refuse_before_credentials_or_watcher_launch(self):
        source = Path(__file__).with_name('cloud-disk-archive.sh').read_text()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            worker = directory / 'cloud-disk-archive.sh'
            worker.write_text(source)
            (directory / 'cloud_disk_work_loop.py').write_text(
                'import sys\nassert sys.argv[1:] == ["recovery-admission"]\nsys.exit(3)\n')
            (directory / 'cloud_disk_credentials.py').write_text(
                'from pathlib import Path\nPath(__file__).with_name("credentials-opened").touch()\n')
            for mode, code in [('recover', 3), ('watch-start', 0), ('watch', 0)]:
                result = subprocess.run(['bash', str(worker), mode], capture_output=True,
                                        text=True, timeout=5)
                self.assertEqual(result.returncode, code, result.stderr)
            self.assertFalse((directory / 'credentials-opened').exists())


if __name__ == '__main__':
    unittest.main()
