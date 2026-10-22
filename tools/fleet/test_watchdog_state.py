import json
import io
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

from watchdog_state import admit, guest_admission, guest_admission_report


class GuestAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.report = dict(disk_free_kib=14*1024**2, memory_free_percent=50,
                           swap_used_mib=8191, cpu_count=10, load_one=9.99)

    def test_exact_vm_budget_passes_but_single_action_budget_fails(self):
        self.assertTrue(guest_admission(self.report))
        self.assertFalse(guest_admission(dict(self.report,memory_free_percent=25,disk_free_kib=8*1024**2)))
        for key,value in [('memory_free_percent',49),('disk_free_kib',14*1024**2-1),
                          ('swap_used_mib',8192),('cpu_count',1),('load_one',10)]:
            self.assertFalse(guest_admission(dict(self.report,**{key:value})))

    def test_missing_boolean_nonfinite_and_malformed_metrics_fail_closed(self):
        for key in self.report:
            row=dict(self.report);del row[key]
            self.assertFalse(guest_admission(row))
            for value in (True,False,None,'50'):
                self.assertFalse(guest_admission(dict(self.report,**{key:value})))
        for row in ([],None,dict(self.report,load_one=float('nan')),
                    dict(self.report,load_one=float('inf')),dict(self.report,memory_free_percent=101),
                    dict(self.report,swap_used_mib=-1)):
            self.assertFalse(guest_admission(row))

    def test_resource_report_is_bounded_and_duplicate_fields_refused(self):
        self.assertTrue(guest_admission_report(io.BytesIO(json.dumps(self.report).encode())))
        for body in (b'x'*65537,b'{"cpu_count":2,"cpu_count":10}',b'{partial'):
            with self.assertRaises(ValueError):guest_admission_report(io.BytesIO(body))

    def test_cli_refuses_without_exposing_report_contents(self):
        script=Path(__file__).with_name('watchdog_state.py')
        result=subprocess.run([sys.executable,str(script),'--guest-admission'],
                              input=b'sensitive-invalid-body',capture_output=True)
        self.assertEqual(result.returncode,75)
        self.assertNotIn(b'sensitive-invalid-body',result.stdout+result.stderr)
        result=subprocess.run([sys.executable,str(script),'--guest-admission'],
                              input=json.dumps(self.report).encode(),capture_output=True)
        self.assertEqual(result.returncode,0)


class RateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_cap_and_window_survive_reopen_and_clock_rollback(self):
        self.assertTrue(admit(self.home, 2, now=1000))
        self.assertTrue(admit(self.home, 2, now=1001))
        self.assertFalse(admit(self.home, 2, now=1002))
        self.assertFalse(admit(self.home, 2, now=999))
        self.assertTrue(admit(self.home, 2, now=4600))
        state = self.home / '.watchdog/rate.json'
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(state.read_text()), {'window_start':4600, 'count':1})

    def test_zero_limit_does_not_create_any_state(self):
        self.assertFalse(admit(self.home, 0))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_symlink_state_does_not_overwrite_target(self):
        admit(self.home, 2, now=1000)
        state = self.home / '.watchdog/rate.json'
        target = self.home / 'protected'
        target.write_text('preserve me')
        state.unlink()
        state.symlink_to(target)
        with self.assertRaises(OSError):
            admit(self.home, 2, now=1001)
        self.assertEqual(target.read_text(), 'preserve me')

    def test_corrupt_or_shell_like_state_fails_closed(self):
        admit(self.home, 2, now=1000)
        state = self.home / '.watchdog/rate.json'
        effect = self.home / 'must-not-exist'
        for body in ('partial', '$(touch ' + str(effect) + ')',
                     '{"window_start":true,"count":0}',
                     '{"window_start":1000,"count":-1}'):
            state.write_text(body)
            with self.assertRaises(ValueError):
                admit(self.home, 2, now=1001)
        self.assertFalse(effect.exists())

    def test_linked_or_shared_state_directory_is_refused(self):
        target = self.home / 'elsewhere'; target.mkdir(mode=0o700)
        state = self.home / '.watchdog'; state.symlink_to(target)
        with self.assertRaises(ValueError):
            admit(self.home, 2)
        state.unlink(); state.mkdir(mode=0o755); state.chmod(0o755)
        with self.assertRaises(ValueError):
            admit(self.home, 2)


if __name__ == '__main__':
    unittest.main()
