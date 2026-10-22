import contextlib
import io
import json
import http.server
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import fleet_push


class TokenFileTests(unittest.TestCase):
    def test_private_literal_env_is_read_without_executing_shell(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'relay.env'
            path.write_text("RELAY_URL=https://example.invalid\nexport AUTH_TOKEN='" + 'k' * 64 + "' # literal\n")
            path.chmod(0o600)
            self.assertEqual(fleet_push.load_token_file(path), 'k' * 64)
            effect = Path(name) / 'executed'
            path.write_text('AUTH_TOKEN=$(touch ' + str(effect) + ')\n')
            with self.assertRaises(ValueError): fleet_push.load_token_file(path)
            self.assertFalse(effect.exists())

    def test_shared_or_linked_credentials_are_refused(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'token'; path.write_text('k' * 64); path.chmod(0o644)
            with self.assertRaises(PermissionError): fleet_push.load_token_file(path)
            path.chmod(0o600)
            alias = Path(name) / 'alias'; alias.symlink_to(path)
            with self.assertRaises(PermissionError): fleet_push.load_token_file(alias)
            alias.unlink(); os.link(path, alias)
            with self.assertRaises(PermissionError): fleet_push.load_token_file(path)

    def test_explicit_file_takes_precedence_over_inherited_secret(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'token'; path.write_text('k' * 64); path.chmod(0o600)
            with patch.dict(os.environ, VOS_AUTH_TOKEN='inherited-private-token'), \
                 patch.object(fleet_push, 'push_once', return_value=0) as push:
                self.assertEqual(fleet_push.main(['--relay','https://relay.invalid','--token-file',str(path)]), 0)
                self.assertEqual(push.call_args.args[2], 'k' * 64)
                self.assertNotIn('VOS_AUTH_TOKEN', os.environ)

class InputDeadlineTests(unittest.TestCase):
    def test_single_shifted_key_is_refused_before_legacy_guest_input(self):
        for key in (':', 'A'):
            command = dict(self.command, action='key', params={'key':key})
            with patch.object(fleet_push, '_get_json', side_effect=[{'command':command}, {'capability':{}}]), \
                 patch.object(fleet_push.time, 'time', return_value=1059), \
                 patch.object(fleet_push, '_push') as applied, \
                 patch.object(fleet_push, '_post_json') as posted:
                fleet_push.control_once('https://relay.invalid', 'http://127.0.0.1:9225', 'private-token', False)
            applied.assert_not_called()
            self.assertEqual(posted.call_args.args[2]['status'], 'refused')

    def test_shifted_text_is_refused_before_guest_input_on_legacy_supervisor(self):
        command = dict(self.command, params={'text':'chrome://version'})
        with patch.object(fleet_push, '_get_json', side_effect=[{'command':command}, {'capability':{}}]), \
             patch.object(fleet_push.time, 'time', return_value=1059), \
             patch.object(fleet_push, '_push') as applied, \
             patch.object(fleet_push, '_post_json') as posted:
            fleet_push.control_once('https://relay.invalid', 'http://127.0.0.1:9225', 'private-token', False)
        applied.assert_not_called()
        self.assertFalse(posted.call_args.args[2]['ok'])
        self.assertEqual(posted.call_args.args[2]['status'], 'refused')

    def test_legacy_type_delivery_finishes_before_cloud_completion(self):
        response = MagicMock(status=200, headers={})
        response.__enter__.return_value = response
        events = []
        with patch.object(fleet_push, '_get_json', return_value={'command': self.command}), \
             patch.object(fleet_push.time, 'time', return_value=1059), \
             patch.object(fleet_push, '_open', return_value=response), \
             patch.object(fleet_push.time, 'sleep', side_effect=lambda delay: events.append(('wait', delay))), \
             patch.object(fleet_push, '_post_json', side_effect=lambda *args: events.append(('completed', args[2]['ok']))):
            fleet_push.control_once('https://relay.invalid', 'http://127.0.0.1:9225', 'private-token', False)
        self.assertEqual(events, [('wait', len(self.command['params']['text']) * .02 + .1), ('completed', True)])

    def test_completed_input_header_avoids_duplicate_settle_delay(self):
        response = MagicMock(status=200, headers={'X-VOS-Input-Completed': '1'})
        response.__enter__.return_value = response
        with patch.object(fleet_push, '_open', return_value=response), \
             patch.object(fleet_push.time, 'sleep') as pause:
            self.assertEqual(fleet_push._push('POST', 'http://127.0.0.1:9225/fleet/input/vm-a', '',
                b'{}', 'application/json', settle_text='abc'), 200)
        pause.assert_not_called()

    def setUp(self):
        self.command = dict(id='00000000-0000-4000-8000-000000000001', vm='vm-a',
                            action='type', params={'text': 'private typed content'},
                            createdAt=1000000, expiresAt=1060000)

    def run_input(self, command, now):
        with patch.object(fleet_push, '_get_json', return_value={'command': command}), \
             patch.object(fleet_push.time, 'time', return_value=now), \
             patch.object(fleet_push, '_push', return_value=200) as applied, \
             patch.object(fleet_push, '_post_json', return_value=200) as posted:
            worked = fleet_push.control_once('https://relay.invalid', 'http://127.0.0.1:9225', 'private-token', False)
        return worked, applied, posted

    def test_expired_input_is_reported_without_guest_action(self):
        worked, applied, posted = self.run_input(self.command, 1060)
        self.assertTrue(worked)
        applied.assert_not_called()
        self.assertEqual(posted.call_args.args[2]['status'], 'expired')

    def test_fresh_input_is_applied_and_result_is_reported(self):
        worked, applied, posted = self.run_input(self.command, 1059)
        self.assertTrue(worked)
        self.assertEqual(json.loads(applied.call_args.args[3])['text'], 'private typed content')
        self.assertTrue(posted.call_args.args[2]['ok'])

    def test_legacy_entries_keep_original_expiry_limit(self):
        command = dict(self.command)
        del command['expiresAt']
        _, applied, posted = self.run_input(command, 1061)
        applied.assert_not_called()
        self.assertEqual(posted.call_args.args[2]['status'], 'expired')

    def test_invalid_deadlines_never_execute(self):
        for value in (True, 'later', float('nan'), float('inf')):
            with self.subTest(value=value):
                _, applied, posted = self.run_input(dict(self.command, expiresAt=value), 1059)
                applied.assert_not_called()
                self.assertEqual(posted.call_args.args[2]['status'], 'refused')

    def test_debug_does_not_log_typed_text_or_token(self):
        output = io.StringIO()
        with patch.dict(os.environ, VOS_CONTROL_DEBUG='1'), contextlib.redirect_stdout(output):
            self.run_input(self.command, 1059)
        self.assertNotIn('private typed content', output.getvalue())
        self.assertNotIn('private-token', output.getvalue())


class FrameUploadTests(unittest.TestCase):
    def test_private_payloads_and_tokens_never_follow_redirects_or_ambient_proxy(self):
        observed = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                self.send_response(307)
                self.send_header('Location', '/other-endpoint')
                self.end_headers()
            def log_message(self, *args): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch.dict(os.environ, HTTP_PROXY='http://127.0.0.1:1', NO_PROXY=''):
                code = fleet_push._push('POST', 'http://127.0.0.1:%d/redirect' % server.server_port,
                                       'fixture-private-token', b'private guest data', 'application/json')
            self.assertEqual(code, 307)
            self.assertEqual(observed, ['/redirect'])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def run_push(self, responses):
        fleet = json.dumps({'instances': [{'name': 'vm-a', 'ready': True}]}).encode()
        with patch.object(fleet_push, '_fetch', side_effect=[(200, fleet, 'application/json'),
                     (200, b'frame', 'image/jpeg')]) as fetched, \
             patch.object(fleet_push, '_push', side_effect=responses) as pushed, \
             patch.object(fleet_push.time, 'time', return_value=2000), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = fleet_push.push_once('https://relay.invalid', 'http://127.0.0.1:9225', 'private-token', False)
        return code, fetched, pushed

    def test_capture_timestamp_precedes_frame_upload(self):
        code, _, pushed = self.run_push([200, 200])
        self.assertEqual(code, 0)
        self.assertEqual(pushed.call_args.kwargs['captured_at'], 2000000)

    def test_failed_frame_upload_is_reported_as_failure(self):
        code, _, _ = self.run_push([200, 503])
        self.assertEqual(code, 1)

    def test_failed_heartbeat_does_not_claim_success(self):
        code, fetched, _ = self.run_push([503])
        self.assertEqual(code, 1)
        self.assertEqual(fetched.call_count, 1)


if __name__ == '__main__':
    unittest.main()
