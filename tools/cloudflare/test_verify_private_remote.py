import http.server
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import verify_private_remote as verifier


class PrivateRemoteTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(REMOTE_URL='https://private.example', CF_ACCESS_CLIENT_ID='test-id',
                             CF_ACCESS_CLIENT_SECRET='test-secret', VOS_AUTH_TOKEN='test-token')

    def test_unsafe_endpoint_and_header_values_fail_before_requests(self):
        changes = ({'REMOTE_URL': 'https://private.example\n.evil'},
                   {'REMOTE_URL': 'http://private.example'},
                   {'REMOTE_URL': 'https://user:secret@private.example'},
                   {'REMOTE_URL': 'https://private.example?token=value'},
                   {'REMOTE_URL': 'https://private.example#fragment'},
                   {'CF_ACCESS_CLIENT_SECRET': 'secret\r\nInjected: value'},
                   {'VOS_AUTH_TOKEN': ''})
        for change in changes:
            with self.subTest(change=change), patch.object(verifier, 'bounded_get') as fetch:
                with self.assertRaises(ValueError):
                    verifier.verify(dict(self.settings, **change), fetch=fetch)
                fetch.assert_not_called()

    def test_unique_private_frame_and_sanitized_result_preserve_old_link_target(self):
        image=b'\xff\xd8'+b'x'*10000+b'\xff\xd9'
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            root=Path(name); target=root/'preserved'; target.write_bytes(b'preserved')
            (root/'virtualos-remote-verify.jpg').symlink_to(target)
            with patch.object(verifier,'bounded_get',side_effect=[b'{"ok":true}',image]) as fetch:
                result=verifier.verify(self.settings,output_root=root,fetch=fetch)
            output=Path(result['frame_file'])
            self.assertEqual(output.read_bytes(),image)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode),0o700)
            self.assertEqual(target.read_bytes(),b'preserved')
            self.assertNotIn('test-secret',str(result)); self.assertNotIn('test-token',str(result))
            self.assertEqual(fetch.call_args.args[2],verifier.MAX_FRAME)

    def test_unhealthy_json_never_requests_a_private_frame(self):
        with patch.object(verifier,'bounded_get',return_value=b'{"ok":false}') as fetch:
            with self.assertRaises(ValueError):
                verifier.verify(self.settings,fetch=fetch)
        self.assertEqual(fetch.call_count,1)

    def test_rejected_frame_never_creates_an_output(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            for image in (b'not-jpeg',b'\xff\xd8'+b'x'*10000):
                with patch.object(verifier,'bounded_get',side_effect=[b'{"ok":true}',image]) as fetch:
                    with self.assertRaises(ValueError):
                        verifier.verify(self.settings,output_root=name,fetch=fetch)
                self.assertEqual(list(Path(name).iterdir()),[])

    def test_absolute_deadline_interrupts_a_slow_successful_stream(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b'x'); self.wfile.flush(); time.sleep(.03)
                except (BrokenPipeError, ConnectionResetError): pass
            def log_message(self,*args): pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            started=time.monotonic()
            with self.assertRaises(TimeoutError), verifier.verification_deadline(.12):
                verifier.bounded_get('http://127.0.0.1:'+str(server.server_address[1]),{},100)
            self.assertLess(time.monotonic()-started,.5)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_real_redirect_never_forwards_credential_and_large_response_is_bounded(self):
        hits=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path=='/redirect':
                    self.send_response(302); self.send_header('Location','/credential-sink'); self.end_headers()
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(b'x'*256)
            def log_message(self,*args): pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            base='http://127.0.0.1:'+str(server.server_address[1])
            with self.assertRaises(ValueError):
                verifier.bounded_get(base+'/redirect',{'Authorization':'Bearer test-token'},32)
            self.assertEqual(hits,['/redirect'])
            with self.assertRaises(ValueError):
                verifier.bounded_get(base+'/large',{},32)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__=='__main__':
    unittest.main()
