from pathlib import Path
import os
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import guest_unlock


class UnlockTests(unittest.TestCase):
    def vm(self, entries):
        image = SimpleNamespace(jpeg=b'image', likely_blank=False, width=1500, height=900)
        return Mock(frame=Mock(return_value=image), ocr=Mock(side_effect=entries),
                    click=Mock(return_value=200), key=Mock(return_value=200))

    def run_unlock(self, vm, *, allowed=True):
        with patch.object(guest_unlock.subprocess, 'run', return_value=SimpleNamespace(returncode=0 if allowed else 1)), \
             patch.object(guest_unlock, 'configured_password', return_value='fixture') as credential:
            result = guest_unlock.unlock(vm, Path('/credential'), Path('/guard'), pause=lambda _: None)
        return result, credential

    def test_password_is_not_read_when_login_is_unconfirmed(self):
        vm = self.vm([[], []])
        result, credential = self.run_unlock(vm)
        self.assertEqual(result['guest_unlock'], 'refused')
        credential.assert_not_called()
        vm.key.assert_not_called()

    def test_unlocked_chrome_desktop_never_submits_a_password(self):
        vm = self.vm([[{'text': 'Chrome', 'x': .04, 'y': .97}]])
        result, credential = self.run_unlock(vm)
        self.assertEqual(result['guest_unlock'], 'already_unlocked')
        credential.assert_not_called()
        vm.key.assert_not_called()

    def test_wake_does_not_bypass_login_confirmation(self):
        vm = self.vm([])
        vm.frame.return_value.likely_blank = True
        result, credential = self.run_unlock(vm)
        self.assertEqual(result['reason'], 'blank_frame')
        credential.assert_not_called()
        vm.key.assert_called_once_with('space')

    def test_pressure_blocks_all_guest_input(self):
        vm = self.vm([])
        result, credential = self.run_unlock(vm, allowed=False)
        self.assertEqual(result['guest_unlock'], 'paused')
        credential.assert_not_called()
        vm.frame.assert_not_called()

    def test_one_submission_requires_independent_desktop_confirmation(self):
        locked = [{'text': '9:32', 'x': .42, 'y': .78},
                  {'text': 'Enter Password', 'x': .45, 'y': .08, 'width': .1, 'height': .03}]
        vm = self.vm([locked, locked, locked, [{'text': 'Finder', 'x': .03, 'y': .96}]])
        result, _ = self.run_unlock(vm)
        self.assertEqual(result['guest_unlock'], 'verified_desktop')
        self.assertEqual([call.args[0] for call in vm.key.call_args_list], list('fixture') + ['return'])
        self.assertEqual(result['credential_route'], 'loopback_only')

    def test_credential_file_permissions_are_enforced(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            path = Path(name) / 'start.env'
            path.write_text('VOS_VM_PASSWORD="fixture123"\n')
            path.chmod(0o600)
            self.assertEqual(guest_unlock.configured_password(path), 'fixture123')
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                guest_unlock.configured_password(path)

    def test_failed_login_is_not_automatically_retried(self):
        locked = [{'text': '9:32', 'x': .42, 'y': .78},
                  {'text': 'Enter Password', 'x': .45, 'y': .08, 'width': .1, 'height': .03}]
        vm = self.vm([locked] * 13)
        result, credential = self.run_unlock(vm)
        self.assertEqual(result['guest_unlock'], 'unverified')
        credential.assert_called_once()
        self.assertEqual([call.args[0] for call in vm.key.call_args_list].count('return'), 1)

    def test_changed_login_frame_prevents_focus_and_secret_read(self):
        locked = [{'text':'9:32','x':.42,'y':.78},
                  {'text':'Enter Password','x':.45,'y':.08,'width':.1,'height':.03}]
        vm = self.vm([locked, [{'text':'Terminal','x':.03,'y':.96}]])
        first = vm.frame.return_value
        changed = SimpleNamespace(jpeg=b'other app', likely_blank=False, width=1500, height=900)
        vm.frame.side_effect = [first, changed]
        result, credential = self.run_unlock(vm)
        self.assertEqual(result['reason'], 'login_frame_changed')
        credential.assert_not_called()
        vm.click.assert_not_called()
        vm.key.assert_not_called()

    def test_lost_prompt_after_focus_never_reads_or_types_secret(self):
        locked = [{'text':'9:32','x':.42,'y':.78},
                  {'text':'Enter Password','x':.45,'y':.08,'width':.1,'height':.03}]
        vm = self.vm([locked, locked, [{'text':'Terminal','x':.03,'y':.96}]])
        result, credential = self.run_unlock(vm)
        self.assertEqual(result['reason'], 'focused_login_unconfirmed')
        credential.assert_not_called()
        vm.click.assert_called_once()
        vm.key.assert_not_called()

    def test_animated_wallpaper_uses_reconfirmed_current_prompt_geometry(self):
        clock = {'text':'9:32','x':.42,'y':.78}
        prompt = {'text':'Enter Password','x':.45,'y':.08,'width':.1,'height':.03}
        moved = dict(prompt, x=.50)
        locked, updated = [clock,prompt], [clock,moved]
        vm = self.vm([locked, updated, updated, [{'text':'Finder','x':.03,'y':.96}]])
        first = vm.frame.return_value
        current = SimpleNamespace(jpeg=b'animated wallpaper', likely_blank=False, width=1500, height=900)
        vm.frame.side_effect = [first,current,current,current]
        result, credential = self.run_unlock(vm)
        self.assertEqual(result['guest_unlock'], 'verified_desktop')
        credential.assert_called_once()
        vm.click.assert_called_once_with(.55*1499, .905*899, frame=current)

    def test_replaced_credential_fifo_is_refused_without_blocking(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            path = Path(name) / 'start.env'
            path.write_text('VOS_VM_PASSWORD=fixture123\n')
            path.chmod(0o600)
            original_open = os.open
            def replace_with_fifo(target, flags):
                path.unlink()
                os.mkfifo(path, 0o600)
                return original_open(target, flags)
            with patch.object(guest_unlock.os, 'open', side_effect=replace_with_fifo):
                with self.assertRaises(ValueError):
                    guest_unlock.configured_password(path)

    def test_shared_directory_oversized_and_duplicate_password_are_refused(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            path = Path(name) / 'start.env'
            path.write_text('VOS_VM_PASSWORD=fixture123\n')
            path.chmod(0o600)
            path.parent.chmod(0o755)
            with self.assertRaises(ValueError):
                guest_unlock.configured_password(path)
            path.parent.chmod(0o700)
            path.write_bytes(b'#' * 65537)
            with self.assertRaises(ValueError):
                guest_unlock.configured_password(path)
            path.write_text('VOS_VM_PASSWORD=fixture123\nVOS_VM_PASSWORD=fixture456\n')
            with self.assertRaises(ValueError):
                guest_unlock.configured_password(path)


if __name__ == '__main__':
    unittest.main()
