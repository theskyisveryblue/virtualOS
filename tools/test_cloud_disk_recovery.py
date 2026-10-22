import json
import io
import subprocess
import sys
import http.server
import threading
import errno
from urllib import error
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import zlib
from types import SimpleNamespace

from cloud_disk import BLOCK, CHUNK, FORMAT, digest
from cloud_disk_recovery import (recover, http_json, resources_allowed,
                                running_fleet_guests, pressure_watch, wait_frame,
                                ResourcePressure, cleanup_owned_proof, pressure_interrupted_network,
                                require_owned_listener, owned_http_json)


class Store:
    def __init__(self, raw):
        encoded = zlib.compress(raw)
        key = digest(encoded)
        body = json.dumps(dict(format=FORMAT, logical_size=len(raw), chunk_size=CHUNK,
            block_size=BLOCK, chunks=[dict(key=key, sha256=digest(raw), raw_size=len(raw),
                                          encoded_size=len(encoded))])).encode()
        self.manifest_sha256 = digest(body)
        self.objects = {'manifest.json': body, 'objects/' + key + '.z': encoded}
        self.gets = []

    def get(self, key, limit):
        self.gets.append(key)
        return self.objects[key]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        # Host storage is an input under test, not a dependency on the operator's
        # real disk becoming full while an approval or background job runs.
        storage=patch('cloud_disk_recovery.os.statvfs',return_value=SimpleNamespace(
            f_bavail=20*1024**3//4096,f_frsize=4096))
        storage.start()
        self.addCleanup(storage.stop)
        self.raw = b'cloud recovery' * 512
        self.raw += bytes((-len(self.raw)) % 512)
        self.store = Store(self.raw)
        self.pins = dict(disk_bytes=len(self.raw), disk_sha256=digest(self.raw))
        self.config = dict(local_cache_limit_bytes=CHUNK, local_overlay_limit_bytes=CHUNK,
                           account_id='test-account', bucket='test-bucket', object_prefix='test-prefix')
        self.temp = tempfile.TemporaryDirectory(prefix='vos-recovery-test-', dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)

    def run_recovery(self, **kwargs):
        with patch('cloud_disk_recovery.subprocess.check_output', return_value='/Applications/virtualOS.app/Contents/MacOS/virtualOS\n'), \
             patch('cloud_disk_recovery.http_json', return_value={'ok': True, 'instances': [{'state': 'running', 'name': 'vm-a'}]}):
            return recover(self.store, self.config, self.store.manifest_sha256,
                           kwargs.get('pins', self.pins), progress=lambda row: None,
                           recovery_root=self.temp.name)

    def test_full_remote_hash_passes_and_capacity_does_not_stop_guests(self):
        result = self.run_recovery()
        self.assertTrue(result['full_r2_readback_verified'])
        self.assertEqual(result['state'], 'waiting_for_capacity')
        self.assertFalse(result['guest_boot_verified'])
        self.assertFalse(result['production_restarted'])
        self.assertEqual(len(self.store.gets), 2)
        self.store.gets.clear()
        self.run_recovery()
        self.assertEqual(self.store.gets, ['manifest.json'])

    def test_wrong_raw_hash_never_publishes_audit(self):
        with self.assertRaisesRegex(ValueError, 'raw disk SHA-256'):
            self.run_recovery(pins=dict(self.pins, disk_sha256='0' * 64))
        self.assertFalse(list(Path(self.temp.name).rglob('base-readback.json')))

    def test_populated_audit_overlay_is_refused(self):
        import sqlite3
        self.run_recovery()
        private = Path(self.temp.name) / self.store.manifest_sha256
        (private / 'base-readback.json').unlink()
        with closing(sqlite3.connect(private / 'readback-overlay.sqlite')) as db:
            with db:
                db.execute('INSERT INTO blocks (idx,data) VALUES (?,?)', (0, bytes(BLOCK)))
        with self.assertRaisesRegex(ValueError, 'overlay must be empty'):
            self.run_recovery()

    def test_storage_pressure_before_audit_does_not_claim_readback(self):
        for code in (errno.ENOSPC,errno.EDQUOT):
            with patch('cloud_disk_recovery.private_directory',side_effect=OSError(code,'full')):
                result=self.run_recovery()
            self.assertEqual(result['state'],'waiting_for_resources')
            self.assertFalse(result['full_r2_readback_verified'])
        self.assertEqual(self.store.gets,[])

    def test_storage_pressure_after_valid_readback_keeps_proven_hash(self):
        with patch('cloud_disk_recovery.subprocess.check_output',side_effect=OSError(errno.ENOSPC,'full')):
            result=recover(self.store,self.config,self.store.manifest_sha256,self.pins,
                           progress=lambda row:None,recovery_root=self.temp.name)
        self.assertTrue(result['full_r2_readback_verified'])
        self.assertEqual(result['state'],'waiting_for_resources')

    def test_io_and_permission_failures_remain_terminal(self):
        for code in (errno.EIO,errno.EACCES):
            with patch('cloud_disk_recovery.private_directory',side_effect=OSError(code,'refused')):
                with self.assertRaises(OSError):self.run_recovery()

    def test_pressure_cannot_mask_disk_or_permission_errors_as_network_abort(self):
        for code in (errno.EIO,errno.EACCES,errno.EPERM,errno.ENOSPC,errno.EDQUOT):
            self.assertFalse(pressure_interrupted_network(OSError(code,'failure')))
        self.assertFalse(pressure_interrupted_network(error.URLError(OSError(errno.EIO,'io'))))
        failure=error.HTTPError('http://localhost',503,'failed',{},io.BytesIO())
        try:
            self.assertFalse(pressure_interrupted_network(failure))
        finally:
            failure.close()
        self.assertTrue(pressure_interrupted_network(error.URLError('connection closed')))
        self.assertTrue(pressure_interrupted_network(OSError(errno.ECONNRESET,'closed')))

    def test_truncated_audit_is_rehashed_and_atomically_replaced(self):
        self.run_recovery()
        audit=Path(self.temp.name)/self.store.manifest_sha256/'base-readback.json'
        audit.write_text('{"partial":')
        self.store.gets.clear()
        self.run_recovery()
        self.assertEqual(len(self.store.gets),2)
        self.assertTrue(json.loads(audit.read_text())['full_r2_readback_verified'])
        self.assertFalse(audit.with_suffix('.json.next').exists())

    def test_reserve_refusal_happens_before_any_remote_read(self):
        with patch('cloud_disk_recovery.os.statvfs',return_value=SimpleNamespace(f_bavail=1,f_frsize=4096)):
            result=self.run_recovery()
        self.assertEqual(result['state'],'waiting_for_resources')
        self.assertFalse(result['full_r2_readback_verified'])
        self.assertEqual(self.store.gets,[])

    def test_wrong_manifest_rejected_before_disk_open(self):
        self.store.manifest_sha256 = '0' * 64
        with self.assertRaisesRegex(ValueError, 'manifest'):
            self.run_recovery()

    def test_unavailable_capacity_probe_fails_closed(self):
        with patch('cloud_disk_recovery.subprocess.check_output', return_value=''), \
             patch('cloud_disk_recovery.http_json', side_effect=OSError('offline')), \
             patch('cloud_disk_recovery.boot_proof') as boot:
            result = recover(self.store, self.config, self.store.manifest_sha256, self.pins,
                             progress=lambda row: None, recovery_root=self.temp.name)
        self.assertEqual(result['state'], 'waiting_for_capacity')
        boot.assert_not_called()

    def test_free_slot_still_requires_host_resource_admission(self):
        with patch('cloud_disk_recovery.subprocess.check_output', return_value=''), \
             patch('cloud_disk_recovery.http_json', return_value={'ok': True, 'instances': []}), \
             patch('cloud_disk_recovery.subprocess.run', return_value=SimpleNamespace(returncode=1)), \
             patch('cloud_disk_recovery.boot_proof') as boot:
            result = recover(self.store, self.config, self.store.manifest_sha256, self.pins,
                             progress=lambda row: None, recovery_root=self.temp.name)
        self.assertEqual(result['state'], 'waiting_for_resources')
        self.assertTrue(result['full_r2_readback_verified'])
        boot.assert_not_called()

    def test_job_directory_symlink_refused(self):
        link = Path(self.temp.name) / 'symlink'
        link.symlink_to(self.temp.name, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'real'):
            recover(self.store, self.config, self.store.manifest_sha256, self.pins,
                    progress=lambda row: None, recovery_root=str(link))

    def test_unknown_capacity_schema_never_admits_a_guest(self):
        for row in ({}, {'ok': True}, {'ok': False, 'instances': []},
                    {'ok': True, 'instances': [{'name': 'vm-a', 'state': 'mystery'}]}):
            with self.assertRaises(ValueError):
                running_fleet_guests(row)
        self.assertEqual(running_fleet_guests({'ok': True, 'instances': [
            {'name': 'vm-a', 'state': 'stopping'}]}), 1)

    def test_four_gib_admission_and_running_pressure_limits(self):
        row = dict(disk_free_kib=15*1024**2, memory_free_percent=55,
                   swap_used_mib=4000, load_one=4, cpu_count=10)
        self.assertTrue(resources_allowed(row))
        self.assertFalse(resources_allowed(dict(row, memory_free_percent=49)))
        self.assertFalse(resources_allowed(dict(row, disk_free_kib=13*1024**2)))
        self.assertTrue(resources_allowed(dict(row, memory_free_percent=35), running=True))
        self.assertFalse(resources_allowed(dict(row, memory_free_percent=29), running=True))
        self.assertFalse(resources_allowed({}))

    def test_pressure_monitor_only_terminates_its_own_child(self):
        stop, aborted = Mock(), threading.Event()
        stop.wait.return_value = False
        own_child = Mock()
        own_child.poll.return_value = None
        with patch('cloud_disk_recovery.resource_report', return_value={}):
            pressure_watch(own_child, stop, aborted)
        self.assertTrue(aborted.is_set())
        own_child.terminate.assert_called_once_with()

    def test_resource_abort_retries_but_real_boot_errors_remain_terminal(self):
        row = dict(disk_free_kib=15*1024**2, memory_free_percent=55,
                   swap_used_mib=4000, load_one=4, cpu_count=10)
        with patch('cloud_disk_recovery.subprocess.check_output', return_value=''), \
             patch('cloud_disk_recovery.http_json', return_value={'ok':True,'instances':[]}), \
             patch('cloud_disk_recovery.resource_report', return_value=row), \
             patch('cloud_disk_recovery.boot_proof', side_effect=ResourcePressure('pressure')):
            result = recover(self.store,self.config,self.store.manifest_sha256,self.pins,
                             progress=lambda row:None,recovery_root=self.temp.name)
        self.assertEqual(result['state'],'waiting_for_resources')
        self.assertTrue(result['full_r2_readback_verified'])
        with patch('cloud_disk_recovery.subprocess.check_output', return_value=''), \
             patch('cloud_disk_recovery.http_json', return_value={'ok':True,'instances':[]}), \
             patch('cloud_disk_recovery.resource_report', return_value=row), \
             patch('cloud_disk_recovery.boot_proof', side_effect=RuntimeError('NBD failure')):
            with self.assertRaisesRegex(RuntimeError,'NBD failure'):
                recover(self.store,self.config,self.store.manifest_sha256,self.pins,
                        progress=lambda row:None,recovery_root=self.temp.name)

    def test_malformed_stop_json_cannot_skip_own_child_cleanup(self):
        child,monitor_stop,monitor,stop,listener,server = (Mock() for _ in range(6))
        child.poll.return_value=None
        server.is_alive.return_value=False
        with patch('cloud_disk_recovery.require_owned_listener'), \
             patch('cloud_disk_recovery.http_json', side_effect=ValueError('malformed JSON')):
            cleanup_owned_proof(child,9257,'private-proof',monitor_stop,monitor,stop,listener,server)
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=30)
        listener.close.assert_called_once_with()
        stop.set.assert_called_once_with()

    def test_listener_cleanup_failure_still_reaps_child(self):
        child,monitor_stop,monitor,stop,listener = (Mock() for _ in range(5))
        child.poll.return_value=None
        listener.close.side_effect=OSError('close failed')
        with patch('cloud_disk_recovery.require_owned_listener'), \
             patch('cloud_disk_recovery.http_json', return_value={}):
            with self.assertRaises(OSError):
                cleanup_owned_proof(child,9257,'private-proof',monitor_stop,monitor,stop,listener,None)
        child.wait.assert_called_once_with(timeout=30)

    def test_control_never_posts_to_an_unowned_listener(self):
        child=Mock(pid=12345)
        with patch('cloud_disk_recovery.require_owned_listener',side_effect=ConnectionError('not owned')), \
             patch('cloud_disk_recovery.http_json') as rpc:
            with self.assertRaises(ConnectionError):
                owned_http_json(child,9257,'/fleet/stop/private-proof','POST')
        rpc.assert_not_called()

    def test_listener_identity_requires_own_pid_and_only_loopback_address(self):
        child=Mock(pid=12345);child.poll.return_value=None
        good='p12345\nf10\nn127.0.0.1:9257\n'
        with patch('cloud_disk_recovery.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=good)):
            require_owned_listener(child,9257)
        for text in ('p54321\nn127.0.0.1:9257\n','p12345\nn*:9257\n',
                     good+'n*:9257\n','p12345\n','x'*65537):
            with self.subTest(text=text[:32]), \
                 patch('cloud_disk_recovery.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=text)):
                with self.assertRaises((ConnectionError,ValueError)):
                    require_owned_listener(child,9257)

    def test_exited_child_never_borrows_a_reused_listener_pid(self):
        child=Mock(pid=12345);child.poll.return_value=0
        with patch('cloud_disk_recovery.subprocess.run') as inspect:
            with self.assertRaises(ConnectionError):require_owned_listener(child,9257)
        inspect.assert_not_called()

    def test_cleanup_skips_foreign_http_but_reaps_its_own_child(self):
        child,monitor_stop,monitor=(Mock() for _ in range(3));child.poll.return_value=None
        with patch('cloud_disk_recovery.require_owned_listener',side_effect=ConnectionError('not owned')), \
             patch('cloud_disk_recovery.http_json') as rpc:
            cleanup_owned_proof(child,9257,'private-proof',monitor_stop,monitor,None,None,None)
        rpc.assert_not_called();child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=30)

    def test_native_listener_check_distinguishes_two_owned_fixture_processes(self):
        code=('import socket,time;s=socket.socket();s.bind(("127.0.0.1",0));'
              's.listen(1);print(s.getsockname()[1],flush=True);time.sleep(15)')
        children=[]
        try:
            for _ in range(2):
                child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,text=True)
                children.append(child)
            ports=[int(child.stdout.readline()) for child in children]
            require_owned_listener(children[0],ports[0])
            with self.assertRaises(ConnectionError):
                require_owned_listener(children[1],ports[0])
            self.assertIsNone(children[1].poll())
        finally:
            for child in children:
                if child.poll() is None:child.terminate()
                child.wait(timeout=5);child.stdout.close()

    def test_frame_timeout_uses_elapsed_budget_not_attempt_count(self):
        now = [0.0]
        def failing_open(*args, **kwargs):
            now[0] += kwargs['timeout']
            raise OSError('offline')
        with patch('cloud_disk_recovery.time.monotonic', side_effect=lambda: now[0]), \
             patch('cloud_disk_recovery.time.sleep', side_effect=lambda seconds: now.__setitem__(0, now[0]+seconds)), \
             patch('cloud_disk_recovery.LOCAL_HTTP.open', side_effect=failing_open) as opened:
            with self.assertRaisesRegex(RuntimeError, '15 minutes'):
                wait_frame(9257, 'private-proof', lambda: None, seconds=25)
        self.assertEqual(now[0], 25)
        self.assertEqual(opened.call_count, 3)

    def test_local_control_never_uses_proxy_or_follows_redirect_and_bounds_json(self):
        paths = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                paths.append(self.path)
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location', '/target'); self.end_headers()
                else:
                    self.send_response(200); self.end_headers()
                    self.wfile.write(b' '*(1024**2+1) if self.path == '/large' else b'{"ok":true}')
        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with patch.dict('os.environ', {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}):
                    self.assertTrue(http_json(server.server_port, '/ok')['ok'])
                    with self.assertRaises(OSError): http_json(server.server_port, '/redirect')
                    with self.assertRaisesRegex(ValueError, 'bound'): http_json(server.server_port, '/large')
            finally:
                server.shutdown(); thread.join(2)
        self.assertNotIn('/target', paths)


if __name__ == '__main__':
    unittest.main()
