from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import relay_read


class RelayReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / '.config/virtualos'
        self.config.mkdir(parents=True, mode=0o700)

    def put(self, name, body):
        path = self.config / name
        path.write_text(body)
        path.chmod(0o600)
        return path

    def test_browser_credential_preferred_without_process_or_shell(self):
        self.put('browser-token', 'b' * 64)
        self.put('relay.env', 'AUTH_TOKEN=' + 'h' * 64)
        with patch.object(relay_read, '_get_json', return_value={'ok':True}) as get:
            self.assertEqual(relay_read.read_status(self.home, {'AUTH_TOKEN':'e' * 64}), {'ok':True})
        self.assertEqual(get.call_args.args, (relay_read.RELAY + '/fleet/status', 'b' * 64))

    def test_literal_host_settings_never_execute(self):
        effect = self.home / 'must-not-exist'
        self.put('relay.env', 'RELAY_URL=$(touch ' + str(effect) + ')\nAUTH_TOKEN=' + 'h' * 64)
        with patch.object(relay_read, '_get_json', return_value={'ok':True}) as get:
            relay_read.read_status(self.home, {})
        self.assertEqual(get.call_args.args[0], relay_read.RELAY + '/fleet/status')
        self.assertFalse(effect.exists())

    def test_invalid_browser_file_never_falls_back_to_host_token(self):
        path = self.put('browser-token', 'b' * 64)
        path.chmod(0o644)
        self.put('relay.env', 'AUTH_TOKEN=' + 'h' * 64)
        with patch.object(relay_read, '_get_json') as get:
            with self.assertRaises(PermissionError):
                relay_read.read_status(self.home, {})
        get.assert_not_called()

    def test_other_endpoint_is_refused_before_loading_or_sending_credential(self):
        with patch.object(relay_read, '_get_json') as get, \
             patch.object(relay_read, 'load_token_file') as load:
            with self.assertRaises(ValueError):
                relay_read.read_status(self.home, {'RELAY_URL':'https://other.invalid'})
        get.assert_not_called()
        load.assert_not_called()

    def test_explicit_host_file_is_used_for_watchdog_even_with_browser_credential(self):
        self.put('browser-token', 'b' * 64)
        host = self.put('relay.env', 'AUTH_TOKEN=' + 'h' * 64)
        with patch.object(relay_read, '_get_json', return_value={'ok':True}) as get:
            relay_read.read_status(self.home, {}, token_path=host)
        self.assertEqual(get.call_args.args[1], 'h' * 64)


if __name__ == '__main__':
    unittest.main()
