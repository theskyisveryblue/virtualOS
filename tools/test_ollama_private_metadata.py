import io
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import ollama_private_metadata as subject


class PrivateMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)

    def inventory(self, names):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for name, kind, link in names:
                row = tarfile.TarInfo(name)
                row.type = kind
                row.linkname = link
                archive.addfile(row)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode='r:') as archive:
            return subject.members(archive)

    def test_reserve_refuses_before_copy_or_stage_creation(self):
        with patch.object(subject, 'source_model', return_value=(b'manifest', {'blob': 20})), \
             patch.object(subject, 'free_bytes', return_value=subject.RESERVE), \
             patch.object(subject.tempfile, 'mkdtemp') as create, \
             patch.object(subject.subprocess, 'run') as run:
            with self.assertRaises(subject.BudgetRefusal):
                subject.stage_and_show()
            create.assert_not_called()
            run.assert_not_called()

    def test_reserve_accounts_for_complete_copy_and_headroom(self):
        with patch.object(subject, 'free_bytes', return_value=subject.RESERVE+subject.HEADROOM+100):
            subject.require_reserve(100)
            with self.assertRaises(subject.BudgetRefusal):
                subject.require_reserve(101)

    def test_unsafe_archive_paths_links_and_special_files_refused(self):
        cases = [
            [('../escape', tarfile.REGTYPE, '')],
            [('/absolute', tarfile.REGTYPE, '')],
            [('a//alias', tarfile.REGTYPE, '')],
            [('a/./alias', tarfile.REGTYPE, '')],
            [('pipe', tarfile.FIFOTYPE, '')],
            [('link', tarfile.SYMTYPE, '../escape')],
            [('a', tarfile.SYMTYPE, 'b'), ('b', tarfile.SYMTYPE, 'a')],
            [('a', tarfile.SYMTYPE, 'file'), ('file', tarfile.REGTYPE, ''), ('a/child', tarfile.REGTYPE, '')],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.inventory(rows)

    def test_internal_relative_library_symlink_is_allowed(self):
        rows, _ = self.inventory([('lib/current', tarfile.SYMTYPE, 'version'),
                                  ('lib/version', tarfile.REGTYPE, '')])
        self.assertEqual(rows['lib/current']['kind'], 'symlink')

    def test_unsafe_store_paths_refused(self):
        store = self.root/'store'; store.mkdir(mode=0o700)
        link = self.root/'linked'; link.symlink_to(store)
        with self.assertRaises(ValueError):
            subject.real_directory(link)
        store.chmod(0o777)
        with self.assertRaises(ValueError):
            subject.real_directory(store)
        with self.assertRaises(ValueError):
            subject.real_directory(Path('relative'))

    def test_archive_checksum_refused_before_extraction(self):
        archive = self.root/'bad.tgz'; archive.write_bytes(b'wrong'); archive.chmod(0o600)
        destination = self.root/'runtime'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            subject.unpack(archive, destination)
        self.assertFalse(destination.exists())

    def test_unpack_emits_self_contained_fixed_inventory_for_future_admission(self):
        archive = self.root/'runtime.tgz'
        with tarfile.open(archive, 'w:gz') as stream:
            for name in ['ollama']+['asset-'+str(i) for i in range(58)]:
                body = b'main' if name == 'ollama' else b'x'
                row = tarfile.TarInfo(name); row.size = len(body)
                row.mode = 0o755 if name == 'ollama' else 0o644
                stream.addfile(row, io.BytesIO(body))
        archive.chmod(0o600)
        archive_sha = subject.hashlib.sha256(archive.read_bytes()).hexdigest()
        with tarfile.open(archive, 'r|gz') as stream:
            rows, total = subject.members(stream)
        for name, row in rows.items():
            row['sha256'] = subject.hashlib.sha256(b'main' if name == 'ollama' else b'x').hexdigest()
        inventory = dict(schema='virtualos-pinned-ollama-runtime-inventory/v1',
                         archive_sha256=archive_sha, decoded_bytes=total, entries=rows)
        with patch.object(subject, 'ARCHIVE_SHA', archive_sha), \
             patch.object(subject, 'ARCHIVE_BYTES', archive.stat().st_size), \
             patch.object(subject, 'DECODED_BYTES', total), \
             patch.object(subject, 'MAIN_SHA', rows['ollama']['sha256']), \
             patch.object(subject, 'RUNTIME_INVENTORY_SHA256', subject.runtime_inventory_digest(inventory)):
            result = subject.unpack(archive, self.root/'extracted')
            saved = json.loads((self.root/'runtime-inventory.json').read_text())
            self.assertEqual(saved, inventory)
            self.assertEqual(subject.verify_runtime(self.root/'extracted', saved)['runtime_files_verified'], 59)
        self.assertEqual(result['runtime_inventory_sha256'], subject.runtime_inventory_digest(saved))

    def runtime_fixture(self):
        root = self.root/'runtime'; root.mkdir(mode=0o700)
        library = root/'lib'; library.mkdir(mode=0o700)
        (root/'ollama').write_bytes(b'main'); (root/'ollama').chmod(0o700)
        (library/'dependent.dylib').write_bytes(b'library'); (library/'dependent.dylib').chmod(0o600)
        (library/'current.dylib').symlink_to('dependent.dylib')
        rows = {'lib': {'kind': 'directory'}}
        for name in ('ollama','lib/dependent.dylib'):
            path = root/name
            rows[name] = dict(kind='file', executable=name=='ollama', bytes=path.stat().st_size,
                              sha256=subject.hashlib.sha256(path.read_bytes()).hexdigest())
        rows['lib/current.dylib'] = dict(kind='symlink', link='dependent.dylib')
        inventory = dict(entries=rows)
        pin = patch.object(subject, 'RUNTIME_INVENTORY_SHA256', subject.runtime_inventory_digest(inventory))
        pin.start(); self.addCleanup(pin.stop)
        return root, inventory

    def test_complete_runtime_inventory_accepts_only_pinned_contents(self):
        root, inventory = self.runtime_fixture()
        checked = subject.verify_runtime(root, inventory)
        self.assertEqual(checked['runtime_files_verified'], 2)
        self.assertEqual(checked['runtime_bytes_verified'], 11)

    def test_dependent_library_tampering_is_refused_even_when_main_is_unchanged(self):
        root, inventory = self.runtime_fixture()
        (root/'lib/dependent.dylib').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'content changed'):
            subject.verify_runtime(root, inventory)

    def test_modified_inventory_cannot_authorize_modified_library(self):
        root, inventory = self.runtime_fixture()
        inventory['entries']['lib/dependent.dylib']['sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'release pin'):
            subject.verify_runtime(root, inventory)

    def test_runtime_extra_file_link_target_and_permission_changes_are_refused(self):
        root, inventory = self.runtime_fixture()
        extra = root/'unexpected'; extra.write_bytes(b'extra'); extra.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            subject.verify_runtime(root, inventory)
        extra.unlink()
        link = root/'lib/current.dylib'; link.unlink(); link.symlink_to('../ollama')
        with self.assertRaisesRegex(ValueError, 'link changed'):
            subject.verify_runtime(root, inventory)
        link.unlink(); link.symlink_to('dependent.dylib')
        (root/'lib/dependent.dylib').chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'permissions changed'):
            subject.verify_runtime(root, inventory)

    def test_runtime_library_hardlink_alias_is_refused(self):
        root, inventory = self.runtime_fixture()
        os.link(root/'lib/dependent.dylib', self.root/'outside-alias')
        with self.assertRaises(ValueError):
            subject.verify_runtime(root, inventory)

    def test_only_metadata_and_exact_digest_show_can_be_requested(self):
        with patch.object(subject.HTTP, 'open') as opened:
            for path, body in [('/api/generate', {}), ('/api/pull', {}),
                               ('/api/show', {'model': 'qwen2.5-coder:0.5b'}),
                               ('/api/version', {'anything': 'secret'})]:
                with self.assertRaises(ValueError):
                    subject.metadata(1234, path, body)
            opened.assert_not_called()

    def test_metadata_failure_reaps_only_own_server(self):
        # Retained private directories from a prior completed attempt are safe
        # to reuse after validation; no existing daemon is attached or reused.
        for name in ('tmp', 'cache'):
            (self.root/name).mkdir(mode=0o700)
        child = Mock(pid=123456, returncode=None)
        def run(args, **kwargs):
            if '/usr/bin/touch' in args:
                target = Path(args[-1])
                if target.parent == self.root:
                    target.touch()
                    return SimpleNamespace(returncode=0)
                return SimpleNamespace(returncode=1)
            if args[0] == '/usr/sbin/lsof':
                port = next(value for value in args if value.startswith('-iTCP:')).split(':')[-1]
                return SimpleNamespace(returncode=0, stdout='ollama 127.0.0.1:'+port)
            raise AssertionError(args)
        def metadata(port, path, body=None):
            if path == '/api/version': return {'version': '0.40.6'}
            if path == '/api/ps': return {'models': []}
            raise ValueError('metadata proof refused')
        with patch.object(subject.subprocess, 'run', side_effect=run), \
             patch.object(subject.subprocess, 'Popen', return_value=child), \
             patch.object(subject, 'metadata', side_effect=metadata), \
             patch.object(subject, 'metadata_admission'), \
             patch.object(subject.os, 'killpg') as signal_group, patch.object(subject.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'metadata proof refused'):
                subject.private_server(self.root)
        self.assertEqual([call.args for call in signal_group.call_args_list],
                         [(123456, signal.SIGTERM), (123456, signal.SIGKILL)])
        child.wait.assert_called_once_with(timeout=5)
        child.poll.assert_not_called()

    def test_retained_stage_logs_cannot_truncate_links_or_block_on_fifo(self):
        target = self.root/'preserved'; target.write_bytes(b'unchanged'); target.chmod(0o600)
        for kind in ('symlink', 'hardlink', 'fifo'):
            path = self.root/('log-'+kind)
            if kind == 'symlink': path.symlink_to(target)
            elif kind == 'hardlink': os.link(target, path)
            else: os.mkfifo(path, 0o600)
            with self.subTest(kind=kind), self.assertRaises((OSError, ValueError)):
                subject.private_output(path)
            self.assertEqual(target.read_bytes(), b'unchanged')

    def test_private_output_reuses_safe_owned_log(self):
        path = self.root/'existing-log'; path.write_bytes(b'previous'); path.chmod(0o600)
        with subject.private_output(path) as stream:
            stream.write(b'fresh')
        self.assertEqual(path.read_bytes(), b'fresh')

    def test_own_group_is_killed_before_leader_reaped(self):
        child = Mock(pid=123456, returncode=None)
        events = []
        child.wait.side_effect = lambda **kwargs: events.append('wait')
        with patch.object(subject.os, 'killpg', side_effect=lambda pid,sig: events.append(sig)), \
             patch.object(subject.time, 'sleep'):
            subject.stop_owned(child)
        self.assertEqual(events, [signal.SIGTERM, signal.SIGKILL, 'wait'])
        child.poll.assert_not_called()

    def test_unreaped_child_is_explicit_cleanup_failure(self):
        child = Mock(pid=123456, returncode=None)
        child.wait.side_effect = subprocess.TimeoutExpired('own', 5)
        with patch.object(subject.os, 'killpg'), patch.object(subject.time, 'sleep'):
            with self.assertRaises(subject.ChildCleanupFailure) as failure:
                subject.stop_owned(child)
        self.assertEqual(failure.exception.child_pid, 123456)

    def test_real_owned_runner_ignoring_term_is_cleaned_and_foreign_child_survives(self):
        sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: (sentinel.terminate(), sentinel.wait(timeout=5)))
        marker = self.root/'runner-ready'
        code = ("import os,signal,time,pathlib\n"
                "child=os.fork()\n"
                "if child==0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                " pathlib.Path("+repr(str(marker))+").touch()\n"
                " while True: time.sleep(1)\n"
                "while not pathlib.Path("+repr(str(marker))+").exists(): time.sleep(.01)\n"
                "print(child,flush=True)\n"
                "while True: time.sleep(1)\n")
        leader = subprocess.Popen([sys.executable, '-c', code], start_new_session=True,
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertTrue(leader.stdout.readline().strip().isdigit())
            subject.stop_owned(leader)
            self.assertIsNotNone(leader.returncode)
            self.assertFalse(subject.owned_group_live(leader.pid))
            self.assertIsNone(sentinel.poll())
        finally:
            if leader.returncode is None:
                os.killpg(leader.pid, signal.SIGKILL); leader.wait(timeout=5)
            leader.stdout.close()

    def test_archive_transfer_has_hard_admitted_file_limit(self):
        with patch.object(subject.resource, 'setrlimit') as limit:
            subject.archive_transfer_limit()
        limit.assert_called_once_with(subject.resource.RLIMIT_FSIZE,
                                      (subject.ARCHIVE_BYTES, subject.ARCHIVE_BYTES))

    def test_unreaped_own_child_keeps_private_stage(self):
        stage = self.root/'retained'; stage.mkdir(mode=0o700)
        archive_info = SimpleNamespace(st_size=subject.ARCHIVE_BYTES)
        with patch.object(subject, 'source_model', return_value=(b'manifest', {})), \
             patch.object(subject, 'require_reserve'), patch.object(subject, 'metadata_admission', return_value={}), patch.object(subject, 'metadata_resources', return_value={}), \
             patch.object(subject, 'global_snapshot', return_value={'loaded': {'models': []}}), \
             patch.object(subject.tempfile, 'mkdtemp', return_value=str(stage)), \
             patch.object(subject.subprocess, 'run', side_effect=lambda *a, **k: (stage/'ollama-darwin.tgz').touch()), \
             patch.object(subject, 'unpack', return_value={}), patch.object(subject, 'copy_model'), \
             patch.object(subject, 'private_server', side_effect=subject.ChildCleanupFailure('unreaped')):
            with self.assertRaises(subject.ChildCleanupFailure):
                subject.stage_and_show()
        self.assertTrue(stage.exists())

    def test_metadata_admission_refuses_swapouts_without_starting_server(self):
        before = dict(swapouts=4)
        after = dict(swapouts=5)
        with patch.object(subject, 'metadata_resources', side_effect=[before, after]), \
             patch.object(subject.time, 'sleep'), patch.object(subject.subprocess, 'Popen') as launch:
            with self.assertRaises(subject.BudgetRefusal):
                subject.private_server(self.root)
            launch.assert_not_called()

    def test_paired_metadata_budget_accepts_stable_swapouts(self):
        with patch.object(subject, 'metadata_resources', return_value={'swapouts': 4}), \
             patch.object(subject.time, 'sleep'), patch.object(subject, 'require_reserve') as reserve:
            self.assertEqual(subject.metadata_admission()['swapouts'], 4)
            reserve.assert_called_once_with()

    def test_metadata_allows_unchanged_global_resident_model_without_inference(self):
        stage = self.root/'resident'; stage.mkdir(mode=0o700)
        snapshot = {'loaded': {'models': [{'name': 'other'}]}}
        with patch.object(subject, 'source_model', return_value=(b'manifest', {})), \
             patch.object(subject, 'require_reserve'), patch.object(subject, 'metadata_admission', return_value={}), \
             patch.object(subject, 'metadata_resources', return_value={}), \
             patch.object(subject, 'global_snapshot', return_value=snapshot), \
             patch.object(subject.tempfile, 'mkdtemp', return_value=str(stage)), \
             patch.object(subject.subprocess, 'run', side_effect=lambda *a, **k: (stage/'ollama-darwin.tgz').touch()), \
             patch.object(subject, 'unpack', return_value={}), patch.object(subject, 'copy_model'), \
             patch.object(subject, 'private_server', return_value={}), patch.object(subject, 'free_bytes', return_value=20*subject.GIB):
            result = subject.stage_and_show()
        self.assertTrue(result['global_daemon_metadata_unchanged'])
        self.assertFalse(result['inference_requested'])


if __name__ == '__main__':
    unittest.main()
