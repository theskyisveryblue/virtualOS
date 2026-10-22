import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from agent_control import Control, ControlError, LeaseError


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.repo = self.root / 'repo'; self.repo.mkdir(mode=0o700)
        self.directory = self.root / 'control'
        self.control = Control(self.directory, self.repo)

    def tearDown(self):
        self.temporary.cleanup()

    def proof(self, name='proof.json', content=b'{"verified":true}'):
        path = self.repo / name; path.write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    def seed(self, *tasks):
        return self.control.seed(list(tasks), now=100)

    def test_read_only_snapshot_does_not_create_directory_or_change_bytes(self):
        self.assertEqual(self.control.snapshot(now=100)['tasks'], [])
        self.assertFalse(self.directory.exists())
        self.seed({'id': 'work'})
        lease = self.control.claim('worker', now=100, ttl=2)
        path = self.directory / 'agent-control.json'
        before = path.read_bytes(); before_stat = path.stat()
        view = self.control.snapshot(now=103)
        self.assertEqual(view['stale_leases'], [lease['token']])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, before_stat.st_mtime_ns)

    def test_concurrent_claim_has_one_worker_and_observer_is_independent(self):
        self.seed({'id': 'a'}, {'id': 'b'}, {'id': 'observe', 'role': 'observer'})
        barrier = threading.Barrier(8); answers = []; failures = []
        def claim(number):
            try:
                barrier.wait()
                answers.append(Control(self.directory, self.repo).claim('owner-' + str(number), now=101))
            except Exception as failure:
                failures.append(failure)
        threads = [threading.Thread(target=claim, args=(number,)) for number in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertFalse(failures)
        self.assertEqual(sum(answer is not None for answer in answers), 1)
        observer = self.control.claim('observer', now=101, role='observer')
        self.assertEqual(observer['task'], 'observe')

    def test_restart_and_expiry_do_not_renew_lease_or_infer_approval(self):
        self.seed({'id': 'approval', 'approval_required': True}, {'id': 'work'})
        lease = self.control.claim('first', now=101, ttl=5)
        restarted = Control(self.directory, self.repo)
        self.assertIsNone(restarted.claim('second', now=105))
        self.assertEqual(restarted.snapshot(now=105)['leases']['worker'], lease)
        replacement = restarted.claim('second', now=106)
        self.assertEqual(replacement['task'], 'work')
        self.assertNotEqual(replacement['token'], lease['token'])
        with self.assertRaises(LeaseError): restarted.heartbeat(lease, now=106)
        self.assertFalse(restarted.snapshot(now=100000)['tasks'][0]['approved'])

    def test_lease_owner_exact_identity_and_bounded_heartbeat(self):
        self.seed({'id': 'work'})
        lease = self.control.claim('owner', now=101)
        fake = dict(lease, owner='other')
        with self.assertRaises(LeaseError): self.control.progress(fake, 'claimed', now=102)
        with self.assertRaises(ControlError): self.control.heartbeat(lease, now=102, ttl=901)
        fresh = self.control.heartbeat(lease, now=102, ttl=3)
        self.assertEqual(fresh['expires_at'], 105)
        with self.assertRaises(LeaseError): self.control.complete(lease, now=103)
        with self.assertRaises(LeaseError): self.control.heartbeat(fresh, now=101)

    def test_completion_requires_observer_evidence_and_source_hashes_remain_bound(self):
        self.seed({'id': 'work'}, {'id': 'next', 'dependencies': ['work']})
        lease = self.control.claim('owner', now=101)
        with self.assertRaises(ControlError): self.control.complete(lease, now=102)
        proof, pin = self.proof()
        source, source_pin = self.proof('source.py', b'print("fixture")')
        with self.assertRaises(ControlError):
            self.control.attest('work', proof, pin, actor='worker', now=102)
        self.control.attest('work', proof, pin, bindings={'source.py': source_pin}, now=102)
        self.control.complete(lease, now=103)
        self.assertEqual(self.control.snapshot(now=103)['tasks'][0]['status'], 'done')
        source.write_bytes(b'changed')
        self.assertFalse(self.control.snapshot(now=104)['tasks'][0]['evidence_valid'])
        view = self.control.reconcile(now=104)
        self.assertEqual(view['tasks'][0]['status'], 'blocked')
        self.assertEqual(view['tasks'][1]['block_reasons'], ['dependency:work'])

    def test_evidence_mutation_between_attest_and_complete_is_refused(self):
        self.seed({'id': 'work'})
        lease = self.control.claim('owner', now=101)
        proof, pin = self.proof()
        self.control.attest('work', proof, pin, now=102)
        proof.write_bytes(b'{"verified":false}')
        with self.assertRaises(ControlError): self.control.complete(lease, now=103)
        self.assertNotEqual(self.control.snapshot(now=103)['tasks'][0]['status'], 'done')

    def test_blocked_head_does_not_hide_eligible_independent_task(self):
        self.seed({'id': 'approval', 'approval_required': True},
                  {'id': 'dependent', 'dependencies': ['approval']},
                  {'id': 'resources', 'resources_ok': False}, {'id': 'independent'})
        lease = self.control.claim('owner', now=101)
        self.assertEqual(lease['task'], 'independent')
        self.control.block(lease, 'resources', now=102)
        self.assertIsNone(self.control.claim('owner', now=103))
        self.control.set_gate('resources', 'resources', True, now=104)
        self.assertEqual(self.control.claim('owner', now=104)['task'], 'resources')

    def test_explicit_observer_import_cannot_bypass_approval_or_dependencies(self):
        self.seed({'id': 'needs-approval', 'approval_required': True},
                  {'id': 'dependent', 'dependencies': ['needs-approval']})
        proof, pin = self.proof()
        with self.assertRaises(ControlError):
            self.control.attest('needs-approval', proof, pin, complete=True, now=101)
        with self.assertRaises(ControlError):
            self.control.attest('dependent', proof, pin, complete=True, now=101)
        self.control.set_gate('needs-approval', 'approval', True, now=102)
        self.control.attest('needs-approval', proof, pin, complete=True, now=103)
        self.control.attest('dependent', proof, pin, complete=True, now=104)
        self.assertTrue(all(task['status'] == 'done' for task in self.control.snapshot(now=104)['tasks']))

    def test_malicious_state_is_never_executed_and_unknown_fields_fail_closed(self):
        self.seed({'id': 'work'})
        marker = self.root / 'must-not-exist'
        path = self.directory / 'agent-control.json'
        state = json.loads(path.read_text())
        state['tasks'][0]['command'] = 'touch ' + str(marker)
        path.write_text(json.dumps(state))
        with self.assertRaises(ControlError): self.control.claim('owner', now=101)
        self.assertFalse(marker.exists())
        path.write_text('__import__("os").system("touch ' + str(marker) + '")')
        with self.assertRaises(ValueError): self.control.snapshot(now=101)
        self.assertFalse(marker.exists())

    def test_symlink_hardlink_fifo_and_broad_state_files_are_refused(self):
        self.seed({'id': 'work'})
        state_path = self.directory / 'agent-control.json'; original = state_path.read_bytes()
        for kind in ('symlink', 'hardlink', 'fifo', 'broad'):
            state_path.unlink()
            target = self.root / ('target-' + kind); target.write_bytes(original); target.chmod(0o600)
            if kind == 'symlink': state_path.symlink_to(target)
            elif kind == 'hardlink': os.link(target, state_path)
            elif kind == 'fifo': os.mkfifo(state_path, 0o600)
            else: state_path.write_bytes(original); state_path.chmod(0o644)
            with self.assertRaises((OSError, ControlError)):
                self.control.snapshot(now=101)
            if kind == 'fifo':
                self.assertTrue(state_path.is_fifo())

    def test_private_directory_and_lock_paths_are_checked(self):
        self.directory.mkdir(mode=0o755)
        with self.assertRaises(PermissionError): Control(self.directory, self.repo)
        self.directory.chmod(0o700)
        target = self.root / 'lock-target'; target.write_bytes(b'untouched')
        (self.directory / 'agent-control.lock').symlink_to(target)
        with self.assertRaises(OSError): self.control.seed([{'id': 'work'}], now=100)
        self.assertEqual(target.read_bytes(), b'untouched')

    def test_evidence_symlink_escape_bad_pin_and_growth_bound_refused(self):
        self.seed({'id': 'work'})
        proof, pin = self.proof()
        link = self.repo / 'linked'; link.symlink_to(proof)
        with self.assertRaises(OSError): self.control.attest('work', link, pin, now=101)
        with self.assertRaises(ControlError): self.control.attest('work', proof, '0' * 64, now=101)
        with self.assertRaises(ControlError):
            self.control.attest('work', proof, pin, bindings={'../proof.json': pin}, now=101)
        large = self.repo / 'large';
        with large.open('wb') as stream: stream.truncate(16 * 1024 * 1024 + 1)
        with self.assertRaises(ControlError): self.control.attest('work', large, pin, now=101)

    def test_missing_or_cyclic_dependencies_refused_without_state_update(self):
        for tasks in [[{'id': 'a', 'dependencies': ['missing']}],
                      [{'id': 'a', 'dependencies': ['b']}, {'id': 'b', 'dependencies': ['a']}]]:
            with self.assertRaises(ControlError): self.control.seed(tasks, now=100)
            self.assertEqual(self.control.snapshot(now=100)['tasks'], [])

    def test_instruction_metadata_survives_restart_and_seed_is_idempotent(self):
        definition = {'id': 'work', 'title': 'Verify source', 'acceptance': 'Exact fixture proof and source hashes.'}
        self.seed(definition); self.seed(definition)
        task = Control(self.directory, self.repo).snapshot(now=101)['tasks'][0]
        self.assertEqual(task['title'], definition['title'])
        self.assertEqual(task['acceptance'], definition['acceptance'])
        self.assertEqual(len(self.control.snapshot(now=101)['tasks']), 1)

    def test_revoked_gate_invalidates_active_lease_and_cannot_be_heartbeated(self):
        self.seed({'id': 'work'})
        lease = self.control.claim('owner', now=101)
        self.control.set_gate('work', 'resources', False, now=102)
        view = self.control.snapshot(now=102)
        self.assertEqual(view['tasks'][0]['status'], 'blocked')
        self.assertEqual(view['leases'], {})
        with self.assertRaises(LeaseError): self.control.heartbeat(lease, now=103)

    def test_invalidated_dependency_completion_propagates_transitively(self):
        self.seed({'id': 'a'}, {'id': 'b', 'dependencies': ['a']}, {'id': 'c', 'dependencies': ['b']})
        proofs = []
        for index, name in enumerate(('a', 'b')):
            path, pin = self.proof(name + '.json')
            self.control.attest(name, path, pin, complete=True, now=101 + index)
            proofs.append(path)
        proofs[0].write_bytes(b'changed')
        before = (self.directory / 'agent-control.json').read_bytes()
        view = self.control.snapshot(now=104)
        self.assertEqual([task['status'] for task in view['tasks']], ['blocked', 'blocked', 'blocked'])
        self.assertEqual((self.directory / 'agent-control.json').read_bytes(), before)
        self.assertIsNone(self.control.claim('owner', now=104))

    def test_old_attempt_evidence_cannot_complete_reclaimed_work(self):
        self.seed({'id': 'work'})
        first = self.control.claim('first', now=101, ttl=2)
        path, pin = self.proof()
        self.control.attest('work', path, pin, now=102)
        second = self.control.claim('second', now=103)
        with self.assertRaises(ControlError): self.control.complete(second, now=104)
        self.control.attest('work', path, pin, now=104)
        self.control.complete(second, now=105)
        self.assertEqual(self.control.snapshot(now=105)['tasks'][0]['status'], 'done')


if __name__ == '__main__':
    unittest.main()
