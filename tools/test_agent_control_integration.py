import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_control import Control
import cloud_disk_work_loop as loop


class ControlIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve(); self.root.chmod(0o700)
        self.repo = self.root / 'repo'; self.repo.mkdir(mode=0o700)
        self.tools = self.repo / 'tools'; self.tools.mkdir(mode=0o700)
        self.receipts = self.tools / 'experiments'; self.receipts.mkdir(mode=0o700)
        self.job = self.root / 'job'; self.job.mkdir(mode=0o700)
        self.source = self.tools / 'cloud_disk_work_loop.py'
        self.source.write_text('# fixture source: never executed\n')
        self.control = Control(self.job / 'agent-control', self.repo)
        self.clock = 1000
        self.patches = [patch.object(loop, 'TOOLS', self.tools),
                        patch('time.time', side_effect=lambda: self.clock),
                        patch.object(loop.subprocess, 'run', side_effect=AssertionError('no subprocesses in controller fixtures'))]
        for item in self.patches: item.start()
        self.source_config = dict(account_id='c24a3fb4bedba669785c18c834f51921',
            bucket='virtualos-cloud-disks', object_prefix='disks/archive-base-0073037110369885')
        self.pins = dict(disk_sha256='b8d7a8499d279c74f3f9c9fa13f91fb498f95f14a08c79c403adf4edd3548321',
            manifest_sha256='40f4506a2a21af31d82bca6c900e5d197842d9dd777cc130aa227e29336f6963',
            logical_bytes=32212254720)

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temporary.cleanup()

    def receipt(self, name, row):
        path = self.receipts / name
        path.write_text(json.dumps(row, sort_keys=True) + '\n'); path.chmod(0o600)
        return path

    def r2_receipts(self):
        self.receipt('r2-archive-base-conversion-20261019.json', dict(self.pins,
            readback_verified=True, chunk_count=7680, archive_bytes=22266108635,
            archive_sha256='0073037110369885595f21c227c6df3f66f65501daca09844a80c38a07d7b711',
            config=self.source_config))
        self.receipt('r2-independent-base-readback-20261019.json', dict(self.pins,
            full_r2_readback_verified=True, source=self.source_config))

    def controller_receipt(self, **overrides):
        row = dict(tests_passed=17, evaluator_review_passed=True,
            source_sha256={'tools/cloud_disk_work_loop.py': hashlib.sha256(self.source.read_bytes()).hexdigest()})
        row.update(overrides)
        return self.receipt('agent-control-verification.json', row)

    def test_pinned_r2_imports_skip_blocked_actions_and_select_controller(self):
        self.r2_receipts()
        summary = loop.control_tick(self.job, claim_worker=True)
        self.assertEqual(summary['completed'], ['archive_conversion', 'r2_readback'])
        self.assertEqual(summary['next_task']['id'], 'controller_improvement')
        self.assertEqual(summary['worker']['owner'], 'codex-worker')
        self.assertIn('approval', summary['blocked']['recovery_boot'])
        self.assertIn('resources', summary['blocked']['ocr_finder'])
        self.assertFalse(summary['executes_arbitrary_commands'])
        state = self.control.snapshot()
        self.assertTrue(all(row['evidence_valid'] for row in state['tasks'][:2]))

    def test_wrong_receipt_pins_never_import_completion(self):
        self.r2_receipts()
        path = self.receipts / 'r2-archive-base-conversion-20261019.json'
        row = json.loads(path.read_text()); row['disk_sha256'] = '0' * 64
        self.receipt(path.name, row)
        summary = loop.control_tick(self.job)
        self.assertEqual(summary['completed'], [])
        self.assertEqual(summary['next_task']['id'], 'archive_conversion')
        self.assertIn('dependency:archive_conversion', summary['blocked']['r2_readback'])

    def test_background_tick_never_renews_live_or_expired_agent_lease(self):
        self.r2_receipts()
        loop.control_tick(self.job, claim_worker=True)
        original = self.control.snapshot()['leases']['worker']
        self.clock += 200
        loop.control_tick(self.job)
        self.assertEqual(self.control.snapshot()['leases']['worker'], original)
        self.clock = original['expires_at']
        summary = loop.control_tick(self.job)
        self.assertIsNone(summary['worker'])
        self.assertEqual(summary['next_task']['id'], 'controller_improvement')
        self.assertFalse(any(task['approved'] for task in self.control.snapshot()['tasks']))

    def test_observer_never_creates_or_writes_state(self):
        self.assertEqual(loop.control_observation(self.job)['completed'], [])
        self.assertFalse((self.job / 'agent-control').exists())
        self.r2_receipts(); loop.control_tick(self.job, claim_worker=True)
        directory = self.job / 'agent-control'
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
        self.clock += 10000
        summary = loop.control_observation(self.job)
        after = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
        self.assertEqual(after, before)
        self.assertEqual(summary['stale_leases'], 1)
        self.assertIsNone(summary['worker'])

    def test_recovery_dispatch_fails_closed_without_state(self):
        report = loop.recovery_admission(self.job)
        self.assertFalse(report['allowed'])
        self.assertEqual(report['block_reasons'], ['task_missing'])
        self.assertFalse((self.job / 'agent-control').exists())

    def test_recovery_requires_current_dependencies_approval_and_resources(self):
        self.control.seed(loop.control_tasks())
        report = loop.recovery_admission(self.job)
        self.assertEqual(set(report['block_reasons']), {'dependency:r2_readback', 'approval', 'resources'})
        self.r2_receipts(); loop.control_tick(self.job)
        self.control.set_gate('recovery_boot', 'approval', True)
        self.assertFalse(loop.recovery_admission(self.job)['allowed'])
        self.control.set_gate('recovery_boot', 'resources', True)
        self.assertTrue(loop.recovery_admission(self.job)['allowed'])
        self.control.set_gate('recovery_boot', 'approval', False)
        self.assertEqual(loop.recovery_admission(self.job)['block_reasons'], ['approval'])

    def test_recovery_refuses_busy_worker_and_invalidated_prerequisite(self):
        self.r2_receipts(); loop.control_tick(self.job, claim_worker=True)
        self.control.set_gate('recovery_boot', 'approval', True)
        self.control.set_gate('recovery_boot', 'resources', True)
        self.assertEqual(loop.recovery_admission(self.job)['block_reasons'], ['worker_busy'])
        self.clock += 301
        self.assertTrue(loop.recovery_admission(self.job)['allowed'])
        path = self.receipts / 'r2-independent-base-readback-20261019.json'
        path.write_text('{}')
        self.assertIn('dependency:r2_readback', loop.recovery_admission(self.job)['block_reasons'])

    def test_source_bound_valid_receipt_completes_current_worker_attempt(self):
        self.r2_receipts(); loop.control_tick(self.job, claim_worker=True)
        lease = self.control.snapshot()['leases']['worker']
        path = self.controller_receipt()
        self.clock += 1
        summary = loop.control_tick(self.job)
        self.assertIn('controller_improvement', summary['completed'])
        self.assertIsNone(summary['worker'])
        task = next(row for row in self.control.snapshot()['tasks'] if row['id'] == 'controller_improvement')
        self.assertEqual(task['evidence']['lease_token'], lease['token'])
        self.assertEqual(task['evidence']['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(task['evidence']['bindings']['tools/cloud_disk_work_loop.py'],
                         hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_mutated_source_reopens_completed_task_and_cannot_import_old_evidence(self):
        self.r2_receipts(); self.controller_receipt()
        loop.control_tick(self.job)
        self.source.write_text('# changed after reviewed proof\n')
        self.clock += 1
        view = loop.control_observation(self.job)
        self.assertNotIn('controller_improvement', view['completed'])
        result = loop.control_tick(self.job)
        self.assertNotIn('controller_improvement', result['completed'])
        self.assertTrue(result.get('receipt_errors'))
        task = next(row for row in self.control.snapshot()['tasks'] if row['id'] == 'controller_improvement')
        self.assertNotEqual(task['status'], 'done')
        self.assertFalse(task['evidence_valid'])

    def test_unreviewed_or_empty_source_receipt_never_completes_work(self):
        self.r2_receipts(); loop.control_tick(self.job, claim_worker=True)
        for overrides in ({'evaluator_review_passed': False}, {'source_sha256': {}}, {'tests_passed': True}):
            self.controller_receipt(**overrides)
            self.assertNotIn('controller_improvement', loop.control_tick(self.job)['completed'])

    def test_malformed_receipt_is_sanitized_and_does_not_stop_independent_work(self):
        self.r2_receipts()
        path = self.receipts / 'agent-control-verification.json'
        path.write_text('INVALID fixture-private-value-do-not-log')
        result = loop.control_tick(self.job, claim_worker=True)
        self.assertEqual(result['next_task']['id'], 'controller_improvement')
        self.assertEqual(result['worker']['task'], 'controller_improvement')
        self.assertTrue(result.get('receipt_errors'))
        self.assertNotIn('fixture-private-value-do-not-log', json.dumps(result))

    def test_malformed_nested_config_and_binding_shapes_do_not_stop_independent_work(self):
        self.r2_receipts()
        path = self.receipts / 'r2-archive-base-conversion-20261019.json'
        original = json.loads(path.read_text())
        self.control.seed(loop.control_tasks())
        # Explicitly block the earlier work so a malformed prerequisite cannot
        # prevent the independent controller task from being selected.
        self.control.set_gate('archive_conversion', 'resources', False)
        for bad_config in (None, [], 'fixture-not-a-config'):
            self.receipt(path.name, dict(original, config=bad_config))
            result = loop.control_tick(self.job, claim_worker=True)
            self.assertEqual(result['receipt_errors']['archive_conversion'], 'receipt_contract_mismatch')
            self.assertNotIn('archive_conversion', result['completed'])
            self.assertNotIn('r2_readback', result['completed'])
            self.assertEqual(result['worker']['task'], 'controller_improvement')
        for bad_bindings in ({'tools/cloud_disk_work_loop.py': None},
                             {'tools/cloud_disk_work_loop.py': []},
                             {'tools/cloud_disk_work_loop.py': True},
                             {'tools/cloud_disk_work_loop.py': 'not-a-digest'}):
            self.controller_receipt(source_sha256=bad_bindings)
            result = loop.control_tick(self.job, claim_worker=True)
            self.assertEqual(result['receipt_errors']['controller_improvement'], 'receipt_contract_mismatch')
            self.assertNotIn('controller_improvement', result['completed'])
            self.assertEqual(result['worker']['task'], 'controller_improvement')


if __name__ == '__main__':
    unittest.main()
