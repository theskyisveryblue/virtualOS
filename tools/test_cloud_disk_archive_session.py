import http.client
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from cloud_disk_archive_session import ArchiveSource, Channel, SigningSession, identity, private, safe_file


class Response:
    status = 206

    def __init__(self, data, offset, total, *, broken=False, etag='pinned'):
        self.body = io.BytesIO(data)
        self.headers = {'Content-Length': str(total - offset),
                        'Content-Range': 'bytes %d-%d/%d' % (offset, total - 1, total), 'ETag': etag}
        self.broken = broken

    def read(self, size):
        if self.broken:
            self.broken = False
            raise http.client.IncompleteRead(self.body.read(3), size - 3)
        return self.body.read(size)

    def close(self):
        self.body.close()


class SessionTests(unittest.TestCase):
    def test_relay_credential_replacement_fifo_is_refused_before_network(self):
        from unittest.mock import Mock
        from cloud_disk_connections import relay_status
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            path = Path(name) / 'token'
            path.write_text('k' * 64); path.chmod(0o600)
            original_open = os.open
            def replace_with_fifo(target, flags):
                path.unlink()
                os.mkfifo(path, 0o600)
                return original_open(target, flags)
            opener = Mock()
            with patch('cloud_disk_connections.os.open', side_effect=replace_with_fifo):
                with self.assertRaises(PermissionError):
                    relay_status(path, opener=opener)
            opener.open.assert_not_called()

    def test_lan_override_retains_config_identity_and_rejects_untrusted_routes(self):
        from cloud_disk_connections import control_options
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            route = directory / 'ssh-route.json'
            route.write_text('{"route":"pinned-lan"}')
            route.chmod(0o600)
            options = control_options(directory)
            self.assertIn('HostName=192.168.1.123', options)
            self.assertIn('ProxyCommand=none', options)
            self.assertFalse(any('HostKey' in item or 'Identity' in item for item in options))
            route.chmod(0o644)
            with self.assertRaises(PermissionError):
                control_options(directory)
            route.chmod(0o600)
            route.write_text('{"route":"unknown"}')
            with self.assertRaises(ValueError):
                control_options(directory)

    def test_shared_ssh_socket_requires_private_owned_directory_and_socket(self):
        from cloud_disk_connections import control_options
        with tempfile.TemporaryDirectory(prefix='vc-', dir='/private/tmp') as name:
            directory = Path(name)
            options = control_options(directory)
            self.assertIn('ControlMaster=auto', options)
            sock = socket.socket(socket.AF_UNIX)
            path = directory / 'ssh-control'
            try:
                sock.bind(str(path))
                path.chmod(0o600)
                self.assertIn('ControlPath=' + str(path), control_options(directory))
                path.chmod(0o666)
                with self.assertRaises(PermissionError):
                    control_options(directory)
            finally:
                sock.close()
                path.unlink()
            path.symlink_to(directory / 'other')
            with self.assertRaises(PermissionError):
                control_options(directory)

    def test_independent_relay_probe_is_read_only_and_does_not_return_credentials(self):
        from unittest.mock import Mock
        from cloud_disk_connections import relay_status
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            token = Path(name) / 'token'
            token.write_text('k' * 64); token.chmod(0o600)
            response = io.BytesIO(json.dumps({'ok': True, 'liveness': {'isStale': False,
                'lastHeartbeatAgeSec': 2}, 'fleet': {'instances': [{'name': 'vm-a', 'ready': True,
                'state': 'running', 'private_extra': 'not returned'}]}}).encode())
            response.status = 200
            opener = Mock(); opener.open.return_value = response
            result = relay_status(token, opener=opener)
            req = opener.open.call_args.args[0]
            self.assertEqual(req.get_method(), 'GET')
            self.assertEqual(req.full_url, 'https://virtualos-relay.ai-c24.workers.dev/fleet/status')
            self.assertFalse(result['isStale'])
            self.assertNotIn('private_extra', json.dumps(result))
            self.assertNotIn('k' * 64, json.dumps(result))
            token.chmod(0o644)
            with self.assertRaises(PermissionError):
                relay_status(token, opener=opener)

    def test_expired_source_capability_renews_at_exact_compressed_offset(self):
        from unittest.mock import Mock
        data = b'abcdef'
        opener = Mock()
        opener.open.side_effect = [Response(data, 0, 6, broken=True),
            HTTPError('https://example.invalid/pinned?old', 403, 'Expired', {}, io.BytesIO()),
            Response(data[3:], 3, 6)]
        renew = Mock(return_value='https://example.invalid/pinned?new')
        source = ArchiveSource('https://example.invalid/pinned?old', 6, opener=opener,
                               renew=renew, pause=lambda _: None)
        self.assertEqual(source.read(5), b'abc')
        self.assertEqual(source.read(3), b'def')
        self.assertEqual(source.position, 6)
        self.assertEqual(opener.open.call_args.args[0].get_header('Range'), 'bytes=3-')
        renew.assert_called_once_with()
        source.close()

    def test_renewal_cannot_change_the_pinned_object(self):
        from unittest.mock import Mock
        for replacement in ('https://other.invalid/pinned?new', 'https://example.invalid/other?new',
                            'https://user:pass@example.invalid/pinned?new'):
            opener = Mock()
            opener.open.side_effect = HTTPError('https://example.invalid/pinned?old', 403, 'Expired', {}, io.BytesIO())
            source = ArchiveSource('https://example.invalid/pinned?old', 6, opener=opener,
                                   renew=lambda: replacement, pause=lambda _: None)
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                source.read(6)

    def test_source_auth_failure_is_immediate_and_does_not_expose_capability(self):
        from unittest.mock import Mock
        opener = Mock()
        opener.open.side_effect = HTTPError('https://private.invalid/?secret=capability', 401,
                                            'Unauthorized', {}, io.BytesIO())
        pause = Mock()
        source = ArchiveSource('https://private.invalid/?secret=capability', 6, opener=opener, pause=pause)
        with self.assertRaisesRegex(PermissionError, 'HTTP 401') as caught:
            source.read(6)
        self.assertNotIn('capability', str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)
        pause.assert_not_called()

    def test_source_does_not_inherit_ambient_proxies(self):
        with patch.dict(os.environ, {'https_proxy': 'http://private-proxy.invalid:8080'}):
            source = ArchiveSource('https://example.invalid/private', 6)
        proxies = [handler.proxies for handler in source.opener.handlers
                   if hasattr(handler, 'proxies')]
        self.assertTrue(all(not proxy for proxy in proxies))

    def test_compressed_range_reconnect_preserves_exact_offset(self):
        from types import SimpleNamespace
        calls = []
        data = b'compressed-archive-data'
        def opened(req, timeout):
            calls.append(req.get_header('Range'))
            offset = int(calls[-1].split('=')[1].split('-')[0])
            return Response(data[offset:], offset, len(data), broken=len(calls) == 1)
        source = ArchiveSource('https://example.invalid/private', len(data),
                               opener=SimpleNamespace(open=opened), pause=lambda _: None)
        result = bytearray()
        while True:
            part = source.read(5)
            if not part:
                break
            result.extend(part)
        source.close()
        self.assertEqual(bytes(result), data)
        self.assertEqual(calls, ['bytes=0-', 'bytes=3-'])

    def test_wrong_range_and_changed_etag_are_refused(self):
        from types import SimpleNamespace
        for wrong in ('range', 'etag'):
            responses = [Response(b'abcdef', 0, 6, broken=True),
                         Response(b'def', 2 if wrong == 'range' else 3, 6,
                                  etag='changed' if wrong == 'etag' else 'pinned')]
            source = ArchiveSource('https://example.invalid/private', 6,
                opener=SimpleNamespace(open=lambda *a, **k: responses.pop(0)), pause=lambda _: None)
            self.assertEqual(source.read(5), b'abc')
            with self.assertRaises(ValueError):
                source.read(3)
            source.close()

    def test_signer_disconnect_resends_same_pending_request(self):
        with tempfile.TemporaryDirectory(prefix='vs-', dir='/private/tmp') as name:
            channel = Channel(Path(name), deadline=time.monotonic() + 10)
            row = {'request': {'id': 7, 'key': 'immutable'}}
            received, failures = [], []
            def worker():
                try:
                    received.append(channel.exchange(row))
                except Exception as failure:
                    failures.append(failure)
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            first = socket.socket(socket.AF_UNIX)
            first.settimeout(3)
            first.connect(str(channel.path))
            with first.makefile('rb') as stream:
                self.assertEqual(json.loads(stream.readline()), row)
            first.close()  # Inject lost SSH before its signature was returned.
            second = socket.socket(socket.AF_UNIX)
            second.settimeout(3)
            second.connect(str(channel.path))
            with second.makefile('rb') as stream:
                self.assertEqual(json.loads(stream.readline()), row)
                second.sendall(b'{"id":7,"headers":{"signed":"bounded"}}\n')
            thread.join(3)
            second.close()
            channel.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(received, [{'id': 7, 'headers': {'signed': 'bounded'}}])

    def test_session_identity_ignores_only_expiring_source_url(self):
        job = dict(config={'prefix': 'a'}, pins={'sha256': 'pin'}, source_url='first')
        self.assertEqual(identity(job), identity(dict(job, source_url='second')))
        self.assertNotEqual(identity(job), identity(dict(job, pins={'sha256': 'different'})))

    def test_unsafe_state_files_refused(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            root = private(Path(name) / 'state')
            target = Path(name) / 'target'
            target.write_text('private')
            (root / 'job.json').symlink_to(target)
            with self.assertRaises(ValueError):
                safe_file(root / 'job.json')
            with self.assertRaises(ValueError):
                private(root / 'job.json')

    def test_operator_transport_reconnects_without_new_job(self):
        session = SigningSession.__new__(SigningSession)
        session.process = None
        session.deadline = time.monotonic() + 10
        session.reconnects = 0
        session.disconnected = lambda: None
        lines = [io.StringIO(''), io.StringIO('{"request":{"id":1}}\n')]
        from types import SimpleNamespace
        def connect():
            session.process = SimpleNamespace(stdout=lines.pop(0))
        def close():
            session.process = None
        with patch.object(session, 'connect', side_effect=connect) as connected, \
             patch.object(session, 'close', side_effect=close), patch('cloud_disk_archive_session.time.sleep'):
            self.assertEqual(json.loads(next(session)), {'request': {'id': 1}})
            self.assertEqual(connected.call_count, 2)
            self.assertEqual(session.reconnects, 1)


if __name__ == '__main__':
    unittest.main()
