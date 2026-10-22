import json
import hashlib
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from linux_lab_work import boot_command, step
import linux_lab_work


class ContinuationTests(unittest.TestCase):
    expected = {key: 'a' * 64 for key in ('kernel_sha256', 'initramfs_sha256', 'package_sha256')}

    def test_pressure_or_unknown_host_never_starts_a_guest(self):
        for code, state in ((75, 'waiting_for_resources'), (255, 'waiting_for_connection'), (1, 'requires_attention')):
            execute = Mock(return_value=SimpleNamespace(returncode=code, stdout=''))
            self.assertEqual(step(execute, self.expected)['state'], state)
            self.assertEqual(execute.call_count, 1)

    def test_pressure_does_not_stage_a_maintained_profile(self):
        stage=Mock()
        execute=Mock(return_value=SimpleNamespace(returncode=75,stdout='{}'))
        self.assertEqual(step(execute,self.expected,boot=stage)['state'],'waiting_for_resources')
        stage.assert_not_called()

    def test_maintained_boot_selects_only_the_private_profile_and_runtime(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            root=Path(name)/'maintained';root.mkdir(mode=0o700)
            tools=root/'runtime/tools';tools.mkdir(parents=True)
            expected={'rootfs_profile_sha256':'a'*64}
            for filename,key in (('Image','kernel_sha256'),('initramfs','initramfs_sha256')):
                body=filename.encode();(root/filename).write_bytes(body)
                expected[key]=hashlib.sha256(body).hexdigest()
            (root/'profile.json').write_text(json.dumps(expected))
            (tools/'linux_maintained_profile.py').write_text('def check(path):\n assert path.name=="profile.json"\n')
            output=root/'selected.json'
            (tools/'linux-persist-lab.sh').write_text(
                'python3 - <<\'PY\'\nimport json,os,pathlib\n'
                'pathlib.Path('+repr(str(output))+').write_text(json.dumps({k:os.environ[k] for k in '
                '("VOS_LINUX_MAINTAINED_PROFILE","VOS_LINUX_HOME","VOS_LINUX_KERNEL","VOS_LINUX_INITRD")}))\nPY\n')
            command=shlex.split(boot_command(expected,artifacts=str(root),
                lab=str(tools/'linux-persist-lab.sh'),maintained=True))
            subprocess.run(command,check=True,capture_output=True)
            selected=json.loads(output.read_text())
            self.assertEqual(selected['VOS_LINUX_MAINTAINED_PROFILE'],str(root/'profile.json'))
            self.assertEqual(selected['VOS_LINUX_HOME'],str(root))
            self.assertEqual(selected['VOS_LINUX_KERNEL'],str(root/'Image'))

    def test_persistence_requires_two_disk_boots_and_durable_marker(self):
        proof = dict.fromkeys(('formatted', 'rootfs_laid_down', 'init_installed', 'root_login_locked',
                               'disk_rooted_boot_1', 'disk_rooted_boot_2', 'persistent'), True)
        for proved in (True, False):
            current = dict(proof, persistent=proved)
            execute = Mock(side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=0,
                stdout='== linux-persist-lab result ==\n' + json.dumps(current) + '\nartifacts under private path')])
            if proved:
                self.assertEqual(step(execute, self.expected)['state'], 'verified')
            else:
                with self.assertRaises(ValueError):
                    step(execute, self.expected)

    def test_resource_loss_during_test_returns_to_wait(self):
        execute = Mock(side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=75)])
        self.assertEqual(step(execute, self.expected)['state'], 'waiting_for_resources')

    def test_resource_wait_keeps_only_whitelisted_measurements_and_reasons(self):
        stdout=json.dumps(dict(disk_free_kib=123,memory_free_percent=38,load_one=9.1,
            reasons=['memory_headroom','private prompt'],secret='never retain'))
        result=step(Mock(return_value=SimpleNamespace(returncode=75,stdout=stdout)),self.expected)
        self.assertEqual(result['resource_gate'],dict(disk_free_kib=123,memory_free_percent=38,
                                                     load_one=9.1,reasons=['memory_headroom']))

    def test_two_boots_with_unlocked_root_do_not_count_as_verified(self):
        proof=dict.fromkeys(('formatted','rootfs_laid_down','init_installed',
                              'disk_rooted_boot_1','disk_rooted_boot_2','persistent'),True)
        proof['root_login_locked']=False
        execute=Mock(side_effect=[SimpleNamespace(returncode=0),SimpleNamespace(returncode=0,
            stdout='== linux-persist-lab result ==\n'+json.dumps(proof))])
        with self.assertRaisesRegex(ValueError,'incomplete'):
            step(execute,self.expected)

    def test_success_exit_without_proof_is_refused(self):
        execute = Mock(side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=0, stdout='')])
        with self.assertRaises(ValueError):
            step(execute, self.expected)

    def test_remote_command_contains_pins_and_only_the_private_lab(self):
        command = boot_command(self.expected)
        self.assertIn('/tools/linux-persist-lab.sh', command)
        self.assertNotIn('kill', command)
        self.assertIn("assert h.hexdigest()==EXPECTED[key]", command)

    def test_guest_lease_survives_loss_of_the_remote_launcher(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            root = Path(name)
            artifacts = root / 'linux/artifacts'; artifacts.mkdir(parents=True)
            tools = root / 'tools'; tools.mkdir()
            expected = {'package_sha256': 'a' * 64}
            for filename, key in (('Image', 'kernel_sha256'), ('initramfs', 'initramfs_sha256')):
                body = filename.encode()
                (artifacts / filename).write_bytes(body)
                expected[key] = hashlib.sha256(body).hexdigest()
            (artifacts / 'ready.json').write_text(json.dumps(expected))
            (tools / 'linux-persist-lab.sh').write_text(
                'state_dir=${VOS_LINUX_KERNEL%/*}\n'
                'touch "$state_dir/started"\n'
                'while test ! -f "$state_dir/release"; do sleep 0.05; done\n')
            with patch.object(linux_lab_work, 'REMOTE', str(root)), \
                 patch.object(linux_lab_work, 'ARTIFACTS', str(artifacts)):
                command = shlex.split(boot_command(expected))
            launcher = subprocess.Popen(command, start_new_session=True,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 3
                while not (artifacts / 'started').exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((artifacts / 'started').exists())
                launcher.terminate(); launcher.wait(timeout=3)
                # The running test shell still holds the inherited lock.
                blocked = subprocess.run(command, capture_output=True, timeout=3)
                self.assertEqual(blocked.returncode, 75)
                (artifacts / 'release').touch()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    retried = subprocess.run(command, capture_output=True, timeout=3)
                    if retried.returncode == 0:
                        break
                    self.assertEqual(retried.returncode, 75)
                    time.sleep(.05)
                self.assertEqual(retried.returncode, 0)
            finally:
                try:
                    os.killpg(launcher.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                launcher.wait(timeout=3)
