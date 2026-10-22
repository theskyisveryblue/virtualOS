import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cloud_disk_credentials import ACCOUNT, ENDPOINT, credentials, read_private_env


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.path = Path(self.tmp.name) / 'r2.env'
        self.path.parent.chmod(0o700)
        self.path.write_text('export CLOUDFLARE_ACCOUNT_ID=' + ACCOUNT + '\n'
                             'CLOUDFLARE_R2_ENDPOINT=' + ENDPOINT + '\n')
        self.path.chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, text):
        self.path.write_text(self.path.read_text() + text)

    def test_account_token_uses_current_verified_id_without_persisting_secret(self):
        token = 't' * 40
        self.append('CLOUDFLARE_API_TOKEN=' + token + '\n')
        self.path.write_text(self.path.read_text().replace('CLOUDFLARE_R2_ENDPOINT=' + ENDPOINT + '\n', ''))
        original = self.path.read_bytes()
        verify = Mock(return_value={'id': 'b' * 32, 'status': 'active'})
        env = credentials(self.path, verify=verify)
        verify.assert_called_once_with(token)
        self.assertEqual(env['AWS_ACCESS_KEY_ID'], 'b' * 32)
        self.assertEqual(env['AWS_SECRET_ACCESS_KEY'], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn('CLOUDFLARE_API_TOKEN', env)
        self.assertEqual(original, self.path.read_bytes())

    def test_literal_s3_credentials_need_no_api_token_or_network(self):
        self.append('AWS_ACCESS_KEY_ID=' + 'a' * 32 + '\nAWS_SECRET_ACCESS_KEY=' + 's' * 64 + '\n')
        self.append('AWS_SESSION_TOKEN=' + 'jwt.' * 10 + '\n')
        verify = Mock()
        env = credentials(self.path, verify=verify)
        self.assertEqual(env['AWS_SECRET_ACCESS_KEY'], 's' * 64)
        self.assertEqual(env['AWS_SESSION_TOKEN'], 'jwt.' * 10)
        verify.assert_not_called()

    def test_wrong_account_and_inactive_token_are_refused(self):
        self.append('CLOUDFLARE_API_TOKEN=' + 't' * 40 + '\n')
        with self.assertRaises(PermissionError):
            credentials(self.path, verify=lambda _: {'id': 'b' * 32, 'status': 'expired'})
        self.path.write_text(self.path.read_text().replace(ACCOUNT, 'f' * 32))
        with self.assertRaises(ValueError):
            credentials(self.path, verify=lambda _: self.fail('wrong account reached API'))

    def test_public_file_symlink_and_hardlink_are_refused(self):
        self.path.chmod(0o644)
        with self.assertRaises(PermissionError):
            read_private_env(self.path)
        self.path.chmod(0o600)
        alias = self.path.with_name('alias')
        alias.symlink_to(self.path)
        with self.assertRaises(PermissionError):
            read_private_env(alias)
        alias.unlink()
        alias.hardlink_to(self.path)
        with self.assertRaises(PermissionError):
            read_private_env(self.path)

    def test_shell_commands_are_never_executed(self):
        marker = self.path.with_name('must-not-exist')
        self.append('CLOUDFLARE_API_TOKEN=$(touch ' + str(marker) + ')\n')
        with self.assertRaises(ValueError):
            read_private_env(self.path)
        self.assertFalse(marker.exists())

    def test_replacement_after_path_check_is_validated_on_open_descriptor(self):
        original_open = os.open
        def replace_with_fifo(path, flags):
            self.path.unlink()
            os.mkfifo(self.path, 0o600)
            return original_open(path, flags)
        with patch('cloud_disk_credentials.os.open', side_effect=replace_with_fifo):
            with self.assertRaises(PermissionError):
                read_private_env(self.path)

    def test_oversized_and_duplicate_assignments_are_refused(self):
        self.append('CLOUDFLARE_ACCOUNT_ID=' + ACCOUNT + '\n')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            read_private_env(self.path)
        self.path.write_bytes(b'#' * 65537)
        with self.assertRaisesRegex(ValueError, 'bound'):
            read_private_env(self.path)


if __name__ == '__main__':
    unittest.main()
