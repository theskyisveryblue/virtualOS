import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import relay_request as transport


class RelayRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.token = 'fixture-token-01234567890123456789'
        self.credentials = self.root / 'relay.env'
        self.credentials.write_text('AUTH_TOKEN=' + self.token + '\n')
        self.credentials.chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    @contextlib.contextmanager
    def server(self, redirect=False, oversized=False):
        rows = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.do_POST()
            def do_POST(self):
                rows.append((self.path, self.headers.get('Authorization'),
                             self.rfile.read(int(self.headers.get('Content-Length', 0)))))
                self.send_response(302 if redirect else 200)
                if redirect:
                    self.send_header('Location', '/must-not-follow')
                self.end_headers()
                self.wfile.write(b'x' * (transport.MAX_RESPONSE + 1) if oversized else b'{"ok":true}')
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield 'http://127.0.0.1:' + str(server.server_port), rows
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_literal_config_is_not_executed_and_auth_stays_in_http_header(self):
        marker = self.root / 'executed'
        self.credentials.write_text('AUTH_TOKEN=' + self.token + '\ntouch ' + str(marker) + '\n')
        with self.server() as (url, rows), patch.object(transport, 'RELAY', url):
            answer = transport.request('POST', '/status', b'{"state":"idle"}',
                                       'application/json', self.credentials, url)
        self.assertEqual(json.loads(answer), {'ok': True})
        self.assertEqual(rows, [('/status', 'Bearer ' + self.token, b'{"state":"idle"}')])
        self.assertFalse(marker.exists())

    def test_redirect_is_not_followed_and_proxy_is_ignored(self):
        with self.server(redirect=True) as (url, rows), patch.object(transport, 'RELAY', url), \
                patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1'}):
            with self.assertRaises(OSError) as caught:
                transport.request('GET', '/cmd/poll', b'', 'application/json', self.credentials, url)
            caught.exception.close()
        self.assertEqual(len(rows), 1)

    def test_wrong_endpoint_route_and_oversized_body_refused_before_open(self):
        for method, path, body, url in [('GET', '/cmd/poll', b'', 'https://other.invalid'),
                ('POST', '/cmd/../result', b'{}', transport.RELAY),
                ('POST', '/status', b'x' * (transport.MAX_BODY + 1), transport.RELAY)]:
            with patch.object(transport, '_open') as opened:
                with self.assertRaises(ValueError):
                    transport.request(method, path, body, 'application/json', self.credentials, url)
                opened.assert_not_called()

    def test_response_is_bounded(self):
        with self.server(oversized=True) as (url, rows), patch.object(transport, 'RELAY', url):
            with self.assertRaises(ValueError):
                transport.request('GET', '/cmd/poll', b'', 'application/json', self.credentials, url)

    def test_cli_absolute_deadline_interrupts_stalled_transport_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGALRM)
        def alarm(seconds):
            signal.setitimer(signal.ITIMER_REAL, 0.03 if seconds else 0)
        arguments = ['relay_request.py', '--token-file', str(self.credentials),
                     '--method', 'GET', '--path', '/cmd/poll']
        with patch('sys.argv', arguments), patch.object(transport.signal, 'alarm', side_effect=alarm), \
                patch.object(transport, 'request', side_effect=lambda *args: time.sleep(1)):
            with self.assertRaisesRegex(SystemExit, 'legacy relay request failed'):
                transport.main()
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_shell_functions_pass_only_token_path_and_stdin_payload(self):
        source = Path(__file__).resolve().parents[1] / 'cloudflare/poller.sh'
        script = source.read_text().split('# ── main loop')[0]
        fixture = self.root / 'tools/cloudflare/poller.sh'
        fixture.parent.mkdir(parents=True)
        fixture.write_text(script + '\npost_result test-id \'{"ok":true}\'\npost_usage_event \'{"kind":"test"}\'\n')
        bindir = self.root / 'bin'; bindir.mkdir()
        fake = bindir / 'python3'
        fake.write_text('#!' + shutil.which('python3') + '\n' +
            'import os,sys,json\n' +
            'with open(os.environ["FIXTURE_RECORD"],"a") as f:f.write(json.dumps({"argv":sys.argv[1:],"body":sys.stdin.read(),"ambient":os.environ.get("AUTH_TOKEN")})+"\\n")\n')
        fake.chmod(0o700)
        records = self.root / 'requests.jsonl'
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'],
                   AUTH_TOKEN=self.token, VOS_AUTH_TOKEN=self.token, FIXTURE_RECORD=str(records),
                   VOS_RELAY_ENV_FILE=str(self.credentials))
        result = subprocess.run(['bash', str(fixture)], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in records.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn(self.token, json.dumps(row))
            self.assertIsNone(row['ambient'])
            self.assertIn(str(self.credentials), row['argv'])
        self.assertEqual(rows[0]['body'], '{"ok":true}')
        self.assertEqual(rows[1]['body'], '{"kind":"test"}')
        self.assertNotIn('auth_header', script)
        self.assertNotIn('source ', script)

    def test_guest_payload_and_json_quote_never_enter_child_argv_or_logs(self):
        source = Path(__file__).resolve().parents[1] / 'cloudflare/poller.sh'
        script = source.read_text().split('# ── main loop')[0]
        fixture = self.root / 'tools/cloudflare/poller.sh'
        fixture.parent.mkdir(parents=True)
        fixture.write_text(script + r'''
payload=$(json_quote "$FIXTURE_SECRET")
vm_command_post /vm/paste "{\"text\":$payload}"
handle_spotlight "$FIXTURE_SECRET"
handle_terminal_exec "$FIXTURE_SECRET"
handle_exec "$FIXTURE_SECRET"
handle_password "$FIXTURE_SECRET"
''')
        bindir = self.root / 'bin'; bindir.mkdir()
        record = self.root / 'curl.jsonl'
        fake = bindir / 'curl'
        fake.write_text('#!' + shutil.which('python3') + '\n' +
            'import os,sys,json\n' +
            'with open(os.environ["FIXTURE_RECORD"],"a") as f:f.write(json.dumps({"argv":sys.argv[1:],"body":sys.stdin.read()})+"\\n")\n')
        fake.chmod(0o700)
        secret = 'fixture-private-payload-quoted-"-$(do-not-execute)'
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'],
                   FIXTURE_SECRET=secret, FIXTURE_RECORD=str(record))
        result = subprocess.run(['bash', str(fixture)], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        rows = [json.loads(line) for line in record.read_text().splitlines()]
        payload_rows = [row for row in rows if '--data-binary' in row['argv']]
        self.assertEqual(len(payload_rows), 5)
        for row in rows:
            self.assertNotIn(secret, json.dumps(row['argv']))
        for row in payload_rows:
            self.assertIn(secret, json.loads(row['body']).values())
            self.assertIn('--noproxy', row['argv'])
            self.assertIn('--max-time', row['argv'])

    def test_fake_deployment_packages_every_python_dependency_without_network(self):
        source = Path(__file__).resolve().parents[1] / 'cloudflare/poller-session.sh'
        bindir = self.root / 'bin'; bindir.mkdir()
        records = self.root / 'deployment.jsonl'
        fake_source = r'''
import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['FIXTURE_RECORD'],'a') as f:
    f.write(json.dumps({'program':name,'argv':args})+'\n')
if name=='openssl':print('0123456789abcdef'*4)
elif name=='wrangler':
    if args==['whoami']:print('ai@damidina.com c24a3fb4bedba669785c18c834f51921')
    elif args==['deploy']:print('https://virtualos-relay.ai-c24.workers.dev')
    elif args[:2]==['secret','put']:sys.stdin.read()
elif name=='ssh':sys.stdin.read()
'''
        for name in ('wrangler', 'openssl', 'ssh', 'scp'):
            fake = bindir / name
            fake.write_text('#!' + shutil.which('python3') + '\n' + fake_source)
            fake.chmod(0o700)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'],
                   HOME=str(self.root), FIXTURE_RECORD=str(records))
        result = subprocess.run(['bash', str(source), 'deploy'], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in records.read_text().splitlines()]
        copies = [row['argv'] for row in rows if row['program'] == 'scp']
        self.assertEqual(len(copies), 2)
        self.assertEqual({Path(path).name for path in copies[1][:-1]},
                         {'relay_request.py', 'relay_read.py', 'fleet_push.py', 'guest_text.py'})
        self.assertTrue(copies[1][-1].endswith('/tools/fleet/'))
        for row in rows:
            self.assertNotIn('0123456789abcdef' * 4, json.dumps(row['argv']))

    def test_password_trace_is_excluded_even_when_training_enabled(self):
        source = Path(__file__).resolve().parents[1] / 'cloudflare/poller.sh'
        script = source.read_text().split('# ── main loop')[0]
        fixture = self.root / 'poller-trace.sh'
        fixture.write_text(script + '\nif traceable_action password; then exit 7; fi\n')
        result = subprocess.run(['bash', str(fixture)], env=dict(os.environ,
            VOS_RELAY_TRAINING_TRACE='1'), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_capture_paths_use_unique_private_files_and_ignore_predictable_path(self):
        source = Path(__file__).resolve().parents[1] / 'cloudflare/poller.sh'
        script = source.read_text().split('# ── main loop')[0]
        fixture = self.root / 'poller-screenshot.sh'
        fixture.write_text(script + r'''
predictable="/tmp/vos_screenshot_$$.jpg"
trap 'rm -f "$predictable"' EXIT
printf sentinel > "$predictable"
capture_frame_to_file() {
  python3 - "$1" <<'PYFIXTURE'
import json,os,pathlib,stat,sys
p=pathlib.Path(sys.argv[1]);info=p.lstat()
assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode)==0o600
with open(os.environ['FIXTURE_RECORD'],'a') as f:f.write(json.dumps(str(p))+'\n')
p.write_bytes(b'fixture-jpeg' * 100)
PYFIXTURE
}
relay_request() { return 0; }
handle_screenshot
handle_screenshot
capture_usage_artifact test-id click before
capture_usage_artifact test-id click before
handle_frame
handle_frame
[[ "$(cat "$predictable")" == sentinel ]]
''')
        records = self.root / 'capture-files.jsonl'
        result = subprocess.run(['bash', str(fixture)], env=dict(os.environ,
            FIXTURE_RECORD=str(records)), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = [Path(json.loads(line)) for line in records.read_text().splitlines()]
        self.assertEqual(len(set(files)), 6)
        self.assertTrue(all(not path.exists() for path in files))

    def test_failed_screenshot_upload_reports_failure_and_removes_own_temp(self):
        source = Path(__file__).resolve().parents[1] / 'cloudflare/poller.sh'
        script = source.read_text().split('# ── main loop')[0]
        fixture = self.root / 'poller-upload-failure.sh'
        fixture.write_text(script + r'''
capture_frame_to_file() {
  printf '%s' "$1" > "$FIXTURE_RECORD"
  printf fixture-jpeg > "$1"
}
relay_request() { return 1; }
handle_screenshot
''')
        record = self.root / 'failed-capture-path'
        result = subprocess.run(['bash', str(fixture)], env=dict(os.environ,
            FIXTURE_RECORD=str(record)), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'ok': False, 'error': 'screenshot upload failed'})
        self.assertFalse(Path(record.read_text()).exists())


if __name__ == '__main__':
    unittest.main()
