import http.server
import json
import socket
import threading
import time
import unittest

from fleet import MultiplexerHandler, MultiplexerServer, backend_address, build_parser


class MuxBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls
        class Backend(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.command, self.path))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'jpeg-fixture' if '/frame/' in self.path else b'{"ok":true}')
            do_POST = do_GET
            def log_message(self, *args):
                pass
        self.backend = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        self.mux = MultiplexerServer(('127.0.0.1', 0), MultiplexerHandler,
                                    'http://127.0.0.1:' + str(self.backend.server_port))
        self.threads = []
        for server in (self.backend, self.mux):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start(); self.threads.append(thread)

    def tearDown(self):
        for server in (self.mux, self.backend):
            server.shutdown(); server.server_close()
        for thread in self.threads:
            thread.join(2)

    def raw(self, data):
        with socket.create_connection(('127.0.0.1', self.mux.server_port), timeout=2) as client:
            client.settimeout(7)
            client.sendall(data)
            result = b''
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    return result
                result += chunk

    def request(self, method, path, headers=''):
        return self.raw((method + ' ' + path + ' HTTP/1.1\r\nHost: 127.0.0.1:' +
                         str(self.mux.server_port) + '\r\n' + headers + '\r\n').encode())

    def test_native_get_post_and_frame_work_without_cors(self):
        for method, path in [('GET', '/fleet/status'), ('GET', '/fleet/capability'),
                             ('POST', '/fleet/start'), ('POST', '/fleet/start/vm-a'), ('POST', '/fleet/stop/vm-a'),
                             ('GET', '/fleet/frame/vm-a?t=123')]:
            response = self.request(method, path, 'Sec-Fetch-Site: none\r\n')
            self.assertTrue(response.startswith(b'HTTP/1.0 200'), response)
            self.assertNotIn(b'Access-Control', response)
        self.assertEqual(self.calls, [('GET', '/fleet/status'), ('GET', '/fleet/capability'),
             ('POST', '/fleet/start'), ('POST', '/fleet/start/vm-a'), ('POST', '/fleet/stop/vm-a'), ('GET', '/fleet/frame/vm-a')])

    def test_browser_origin_and_fetch_metadata_never_reach_backend(self):
        for headers in ['Origin: https://evil.invalid\r\n', 'Origin: null\r\n',
                        'Origin:\r\n', 'Sec-Fetch-Site: cross-site\r\n',
                        'Sec-Fetch-Site: same-origin\r\n',
                        'Sec-Fetch-Site: none\r\nSec-Fetch-Site: none\r\n']:
            response = self.request('POST', '/fleet/start', headers)
            self.assertIn(b' 403 ', response)
            self.assertNotIn(b'Access-Control', response)
        self.assertEqual(self.calls, [])

    def test_wrong_missing_and_duplicate_host_never_reach_backend(self):
        for host in ['evil.invalid:' + str(self.mux.server_port),
                     '127.0.0.1:1', 'localhost', '127.0.0.1:' + str(self.mux.server_port) +
                     '\r\nHost: 127.0.0.1:' + str(self.mux.server_port)]:
            response = self.raw(('POST /fleet/start HTTP/1.1\r\nHost: ' + host + '\r\n\r\n').encode())
            self.assertIn(b' 403 ', response)
        self.assertIn(b' 403 ', self.raw(b'POST /fleet/start HTTP/1.0\r\n\r\n'))
        self.assertEqual(self.calls, [])

    def test_localhost_native_host_is_allowed(self):
        response = self.raw(('GET /fleet/status HTTP/1.1\r\nHost: localhost:' +
                            str(self.mux.server_port) + '\r\n\r\n').encode())
        self.assertIn(b' 200 ', response)
        self.assertEqual(self.calls, [('GET', '/fleet/status')])

    def test_framing_and_options_are_refused_before_backend(self):
        for headers in ['Content-Length: 1\r\n', 'Content-Length: -1\r\n',
                        'Content-Length: 0\r\nContent-Length: 0\r\n',
                        'Transfer-Encoding: chunked\r\n', 'Expect: 100-continue\r\n']:
            self.assertIn(b' 400 ', self.request('POST', '/fleet/start', headers))
        self.assertIn(b' 405 ', self.request('OPTIONS', '/fleet/start'))
        self.assertEqual(self.calls, [])

    def test_oversized_headers_and_unknown_routes_never_reach_backend(self):
        response = self.request('POST', '/fleet/start', 'X-Pad: ' + 'a' * 9000 + '\r\n')
        self.assertIn(b' 431 ', response)
        for path in ['/fleet/start/vm-a/extra', '/fleet/start-anything', '/fleet/stop/../start',
                     '/fleet/status?unexpected=1']:
            self.assertIn(b' 404 ', self.request('POST', path))
        self.assertEqual(self.calls, [])

    def test_segmented_request_works_and_stalled_headers_have_absolute_deadline(self):
        with socket.create_connection(('127.0.0.1', self.mux.server_port), timeout=2) as client:
            client.settimeout(7)
            client.sendall(b'GET /fleet/status HTTP/1.1\r\nHo')
            time.sleep(0.02)
            client.sendall(('st: 127.0.0.1:' + str(self.mux.server_port) + '\r\n\r\n').encode())
            self.assertIn(b' 200 ', client.recv(1024))
        before = time.monotonic()
        with socket.create_connection(('127.0.0.1', self.mux.server_port), timeout=2) as client:
            client.settimeout(7)
            client.sendall(b'POST /fleet/start HTTP/1.1\r\nHost: ')
            self.assertEqual(client.recv(1024), b'')
        self.assertLess(time.monotonic() - before, 6.5)
        self.assertEqual(self.calls, [('GET', '/fleet/status')])

    def test_non_loopback_bind_and_backend_refused_before_listening(self):
        self.assertEqual(build_parser().parse_args(['serve']).host, '127.0.0.1')
        for host in ['0.0.0.0', '192.168.1.123', '::']:
            with self.assertRaises(ValueError):
                MultiplexerServer((host, 0), MultiplexerHandler, 'http://127.0.0.1:9200')
        for url in ['https://evil.invalid', 'http://127.0.0.1:9200@evil.invalid',
                    'http://localhost:9200/path', 'http://localhost:9200?x=1']:
            with self.assertRaises(ValueError):
                backend_address(url)


if __name__ == '__main__':
    unittest.main()
