"""Test a supplied scratch supervisor; never registers or starts a real VM."""
import http.client
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest


class NativeHTTPBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='vos-native-http-', dir='/private/tmp')
        cls.root = Path(cls.temp.name)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            cls.port = probe.getsockname()[1]
        cls.log = (cls.root/'supervisor.log').open('wb')
        cls.child = subprocess.Popen([BINARY, '--root', str(cls.root), '--names', 'vm-a',
                                      '--http-port', str(cls.port)], stdin=subprocess.DEVNULL,
                                     stdout=cls.log, stderr=cls.log,
                                     env={k:v for k,v in os.environ.items()
                                          if k in ('PATH','HOME','USER','LOGNAME','LANG')})
        cls.addClassCleanup(cls.cleanup)
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            if cls.child.poll() is not None:
                raise RuntimeError('scratch supervisor exited before readiness')
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=.5):
                    return
            except OSError:
                time.sleep(.1)
        raise RuntimeError('scratch supervisor readiness timeout')

    @classmethod
    def cleanup(cls):
        if cls.child.poll() is None:
            cls.child.terminate()
        try:
            cls.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.child.kill(); cls.child.wait(timeout=5)
        cls.log.close()
        cls.temp.cleanup()

    def exchange(self, headers='', body=b'', request='GET /fleet/health HTTP/1.1', host=None):
        host = '127.0.0.1:'+str(self.port) if host is None else host
        raw = (request+'\r\nHost: '+host+'\r\n'+headers+'\r\n').encode()+body
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as client:
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(client)
            response.begin()
            data = response.read(1048577)
            return response.status, dict(response.getheaders()), data

    def test_native_health_succeeds_without_browser_cors(self):
        status, headers, data = self.exchange()
        self.assertEqual(status, 200)
        self.assertIn(b'"ok":true', data)
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_browser_origins_and_cross_site_fetch_are_refused(self):
        for headers in ('Origin: https://untrusted.invalid\r\n',
                        'Origin: null\r\n', 'Sec-Fetch-Site: cross-site\r\n'):
            self.assertEqual(self.exchange(headers)[0], 403)

    def test_host_mismatch_duplicates_and_absolute_targets_are_refused(self):
        self.assertEqual(self.exchange(host='untrusted.invalid:'+str(self.port))[0], 403)
        self.assertEqual(self.exchange('Host: localhost:'+str(self.port)+'\r\n')[0], 403)
        self.assertEqual(self.exchange(request='GET http://untrusted.invalid/ HTTP/1.1')[0], 403)

    def test_ambiguous_or_oversized_body_framing_is_refused_before_routing(self):
        for headers in ('Transfer-Encoding: chunked\r\n', 'Content-Length: -1\r\n',
                        'Content-Length: 0\r\nContent-Length: 0\r\n',
                        'Content-Length: 1048577\r\n', 'Content-Length: 1x\r\n'):
            self.assertEqual(self.exchange(headers, request='POST /fleet/input/vm-a HTTP/1.1')[0], 403)

    def test_oversized_headers_and_truncated_body_are_refused(self):
        self.assertEqual(self.exchange('X-Padding: '+'x'*9000+'\r\n')[0], 403)
        self.assertEqual(self.exchange('Content-Length: 30\r\n', b'{}',
                                      request='POST /fleet/input/vm-a HTTP/1.1')[0], 400)

    def test_segmented_native_request_still_succeeds(self):
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as client:
            client.sendall(b'GET /fleet/health HTTP/1.1\r\nHo')
            time.sleep(.03)
            client.sendall(('st: 127.0.0.1:'+str(self.port)+'\r\n\r\n').encode())
            response = http.client.HTTPResponse(client); response.begin()
            self.assertEqual(response.status, 200)

    def test_invalid_origin_is_refused_before_waiting_for_body(self):
        began = time.monotonic()
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as client:
            client.sendall(('POST /fleet/input/vm-a HTTP/1.1\r\nHost: 127.0.0.1:'
                            +str(self.port)+'\r\nOrigin: https://untrusted.invalid\r\n'
                            'Content-Length: 1048576\r\n\r\n').encode())
            response = http.client.HTTPResponse(client); response.begin()
            self.assertEqual(response.status, 403)
        self.assertLess(time.monotonic()-began, 2)

    def test_body_read_uses_remaining_deadline(self):
        began = time.monotonic()
        with socket.create_connection(('127.0.0.1', self.port), timeout=7) as client:
            client.sendall(('POST /fleet/input/vm-a HTTP/1.1\r\nHost: 127.0.0.1:'
                            +str(self.port)+'\r\nContent-Length: 1\r\n').encode())
            time.sleep(4.5)
            client.sendall(b'\r\n')
            response = http.client.HTTPResponse(client); response.begin()
            self.assertEqual(response.status, 400)
        self.assertLess(time.monotonic()-began, 7)

    def test_abandoned_clients_cannot_terminate_supervisor(self):
        for _ in range(10):
            with socket.create_connection(('127.0.0.1', self.port), timeout=3) as client:
                client.sendall(('GET /fleet/status HTTP/1.1\r\nHost: 127.0.0.1:'
                                +str(self.port)+'\r\n\r\n').encode())
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii',1,0))
        time.sleep(.1)
        self.assertIsNone(self.child.poll())
        self.assertEqual(self.exchange()[0], 200)

    def test_slow_header_cannot_hold_connection_indefinitely(self):
        began = time.monotonic()
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as client:
            for _ in range(80):
                try:
                    client.sendall(b'G')
                except OSError:
                    break
                time.sleep(.1)
            else:
                self.fail('slow header remained open beyond the request deadline')
        self.assertLess(time.monotonic()-began, 8)
        self.assertEqual(self.exchange()[0], 200)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: native_http_boundary_test.py SCRATCH_SUPERVISOR')
    BINARY = str(Path(sys.argv[1]).resolve(strict=True))
    unittest.main(argv=[sys.argv[0]], verbosity=2)
