import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_control import Control
import storage_pruning_job as job


class PruningJobTests(unittest.TestCase):
    def fixture(self, directory):
        control = Control(directory / 'agent-control', job.TOOLS.parent)
        control.seed([dict(id='overlay_pruning_audit', title='Audit', resources_ok=False)])
        return control

    def result(self):
        return dict(state='verified', temporary_snapshot_released=True,
                    original_identity_unchanged=True, protected_processes_unchanged=True,
                    guest_services_changed=False,
                    audit=dict(read_only=True, closed_snapshot=True, actual_reclaimed_bytes=0,
                               blocks=3, redundant_blocks=1),
                    remote_program_sha256=hashlib.sha256(
                        (job.TOOLS / 'storage_pruning_remote.py').read_bytes()).hexdigest(),
                    source_sha256={name: hashlib.sha256((job.TOOLS / name).read_bytes()).hexdigest()
                                   for name in job.SOURCE_NAMES})

    def test_resource_wait_retries_after_five_minutes_and_success_error_stop(self):
        for state in ('waiting_for_resources', 'waiting_for_connection', 'running'):
            self.assertFalse(job.should_start(dict(state=state, updated_at=100), 399))
            self.assertTrue(job.should_start(dict(state=state, updated_at=100), 400))
        for state in ('verified', 'requires_attention'):
            self.assertFalse(job.should_start(dict(state=state), 10000))

    def test_missing_queue_approval_and_active_worker_refuse_dispatch(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name).resolve()
            self.assertFalse(job.eligible(directory))
            control = self.fixture(directory)
            self.assertTrue(job.eligible(directory))
            control.seed([dict(id='other_work', title='Other')])
            control.claim('fixture', ttl=300)
            self.assertFalse(job.eligible(directory))

    def test_waiting_result_does_not_complete_or_relax_gate(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name).resolve()
            control = self.fixture(directory)
            self.assertFalse(job.accept(directory, dict(state='waiting_for_resources')))
            self.assertIn('resources', control.snapshot()['tasks'][0]['block_reasons'])

    def test_pinned_success_completes_without_agent_claim_or_heartbeat(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name).resolve()
            control = self.fixture(directory)
            with patch.object(Control, 'claim', side_effect=AssertionError('background claim')), \
                 patch.object(Control, 'heartbeat', side_effect=AssertionError('background heartbeat')):
                self.assertTrue(job.accept(directory, self.result()))
            row = control.snapshot()['tasks'][0]
            self.assertEqual(row['status'], 'done')
            self.assertTrue(row['evidence_valid'])

    def test_wrong_pin_or_counts_never_clear_resource_gate(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name).resolve()
            control = self.fixture(directory)
            for result in (dict(self.result(), source_sha256={}),
                           dict(self.result(), audit=dict(self.result()['audit'], redundant_blocks=9))):
                with self.assertRaisesRegex(ValueError, 'contract'):
                    job.accept(directory, result)
                self.assertIn('resources', control.snapshot()['tasks'][0]['block_reasons'])

    def test_remote_payload_is_fixed_code_and_python_syntax_valid(self):
        compile(job.remote_payload(), '<pruning remote payload>', 'exec')
        self.assertIn('remote_program_sha256', job.remote_payload())


if __name__ == '__main__':
    unittest.main()
