"""Exercise session status without real screen sessions or network access."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


class PollerSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1]
        self.script = self.root / 'tools/cloudflare/poller-session.sh'
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(source / 'cloudflare/poller-session.sh', self.script)
        fleet = self.root / 'tools/fleet'
        fleet.mkdir()
        self.capture = self.root / 'reader-call.json'
        (fleet / 'relay_read.py').write_text('''import json, os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, SOURCE)
import relay_read
Path(CAPTURE).write_text(json.dumps({'argv':sys.argv[1:],
    'credential_in_environment':any(os.environ.get(k) for k in ('AUTH_TOKEN','VOS_AUTH_TOKEN'))}))
with patch.object(relay_read, '_get_json', return_value={
        'ok':True,'liveness':{'isStale':False,'lastHeartbeatAgeSec':0}}):
    print(json.dumps(relay_read.read_status(token_path=sys.argv[2])))
'''.replace('SOURCE', repr(str(source / 'fleet'))).replace('CAPTURE', repr(str(self.capture))))
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('screen', 'pgrep'):
            executable = self.bin / name
            executable.write_text('#!/bin/sh\nexit 1\n')
            executable.chmod(0o700)
        python = self.bin / 'python3'
        python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        python.chmod(0o700)
        config = self.root / 'private'
        config.mkdir(mode=0o700)
        self.effect = self.root / 'must-not-exist'
        self.settings = config / 'relay.env'
        self.settings.write_text('AUTH_TOKEN=' + 's'*64 + '\nUNTRUSTED=$(touch '
                                 + str(self.effect) + ')\n')
        self.settings.chmod(0o600)

    def status(self):
        env = dict(os.environ, PATH=str(self.bin) + ':/usr/bin:/bin',
                   VOS_RELAY_ENV_FILE=str(self.settings), AUTH_TOKEN='inherited-fixture',
                   VOS_AUTH_TOKEN='inherited-fixture')
        env.pop('RELAY_URL', None)
        return subprocess.run(['bash', str(self.script), 'status'], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_status_uses_literal_private_reader_without_secret_argv_or_environment(self):
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('relay fleet: live', result.stdout)
        self.assertFalse(self.effect.exists())
        call = json.loads(self.capture.read_text())
        self.assertEqual(call['argv'], ['--token-file', str(self.settings)])
        self.assertFalse(call['credential_in_environment'])
        self.assertNotIn('s'*64, result.stdout + result.stderr)

    def test_unsafe_settings_fail_without_shell_execution_or_credential_fallback(self):
        self.settings.chmod(0o644)
        result = self.status()
        self.assertEqual(result.returncode, 0)
        self.assertIn('unreachable or unauthorized', result.stdout)
        self.assertFalse(self.effect.exists())
        self.assertNotIn('inherited-fixture', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
