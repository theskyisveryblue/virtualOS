import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = self.root / 'tools'
        fleet = self.tools / 'fleet'; fleet.mkdir(parents=True)
        self.binary = self.root / 'bin'; self.binary.mkdir()
        self.actions = self.root / 'actions'
        self.effect = self.root / 'must-not-exist'
        relay = self.root / 'relay.env'
        relay.write_text('AUTH_TOKEN=$(touch ' + str(self.effect) + ')\n'); relay.chmod(0o600)
        self.env = dict(os.environ, VOS_FLEET_HOME=str(self.root), VOS_FLEET_TOOLS=str(self.tools),
                        VOS_RELAY_ENV=str(relay), VOS_TEST_ACTIONS=str(self.actions),
                        VOS_TEST_SUP_UP='0', VOS_TEST_VM_STATE='idle', VOS_TEST_STALE='1',
                        VOS_WD_MAX_ACTIONS='10', PATH=str(self.binary) + ':' + os.environ['PATH'])
        self.write(self.binary / 'curl', '#!/bin/bash\n[[ "$VOS_TEST_SUP_UP" == 1 ]] || exit 7\nprintf \'{"instances":[{"state":"%s"}]}\' "$VOS_TEST_VM_STATE"\n')
        self.write(self.binary / 'pgrep', '#!/bin/bash\nexit 1\n')
        self.write(self.binary / 'launchctl', '#!/bin/bash\necho launchctl >> "$VOS_TEST_ACTIONS"\n')
        self.write(self.tools / 'computer-resources.sh', '#!/bin/bash\necho guard >> "$VOS_TEST_ACTIONS"\nexit 75\n')
        self.write(fleet / 'go-live.sh', '#!/bin/bash\necho unexpected-guest >> "$VOS_TEST_ACTIONS"\n')
        self.write(fleet / 'runbook.sh', '#!/bin/bash\necho unexpected-guest >> "$VOS_TEST_ACTIONS"\n')
        self.write(fleet / 'fleet_push.py', 'import os\nwith open(os.environ["VOS_TEST_ACTIONS"],"a") as f: f.write("push\\n")\n')
        self.write(fleet / 'relay_read.py', 'import os,json\nprint(json.dumps({"liveness":{"isStale":os.environ["VOS_TEST_STALE"]=="1","lastHeartbeatAgeSec":20}}))\n')
        shutil.copyfile(Path(__file__).with_name('watchdog_state.py'), fleet / 'watchdog_state.py')

    def write(self, path, body):
        path.write_text(body); path.chmod(0o700)

    def run_watchdog(self):
        result = subprocess.run(['bash', str(Path(__file__).with_name('fleet-watchdog.sh')), '--once'],
                                env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.effect.exists(), 'relay settings were shell-executed')
        return self.actions.read_text().splitlines() if self.actions.exists() else []

    def test_pressure_blocks_supervisor_recovery_without_consuming_action_budget(self):
        self.assertEqual(self.run_watchdog(), ['guard'])
        self.assertFalse((self.root / '.watchdog').exists())

    def test_pressure_blocks_vm_start_without_consuming_action_budget(self):
        self.env['VOS_TEST_SUP_UP'] = '1'
        self.assertEqual(self.run_watchdog(), ['guard'])
        self.assertFalse((self.root / '.watchdog').exists())

    def test_live_relay_never_recovers_or_reads_credentials_in_shell(self):
        self.env['VOS_TEST_STALE'] = '0'
        self.assertEqual(self.run_watchdog(), [])

    def test_healthy_guest_allows_transport_recovery_without_guest_change(self):
        self.env.update(VOS_TEST_SUP_UP='1', VOS_TEST_VM_STATE='running')
        self.assertEqual(self.run_watchdog(), ['launchctl', 'push'])

    def test_zero_action_limit_guarantees_monitor_only_with_stale_relay(self):
        self.env.update(VOS_TEST_SUP_UP='1', VOS_TEST_VM_STATE='running', VOS_WD_MAX_ACTIONS='0')
        self.assertEqual(self.run_watchdog(), [])

    def test_single_action_headroom_cannot_admit_a_vm(self):
        self.write(self.tools/'computer-resources.sh', '#!/bin/bash\n'
                   '[[ "$1" == --report ]] || exit 99\necho report >> "$VOS_TEST_ACTIONS"\n'
                   'echo \'{"disk_free_kib":8388608,"memory_free_percent":25,"swap_used_mib":4000,"cpu_count":10,"load_one":4}\'\n')
        self.assertEqual(self.run_watchdog(),['report'])
        self.assertFalse((self.root/'.watchdog').exists())

    def test_full_vm_budget_can_admit_only_the_isolated_fixture(self):
        self.write(self.tools/'computer-resources.sh', '#!/bin/bash\n'
                   '[[ "$1" == --report ]] || exit 99\necho report >> "$VOS_TEST_ACTIONS"\n'
                   'echo \'{"disk_free_kib":14680064,"memory_free_percent":50,"swap_used_mib":4000,"cpu_count":10,"load_one":4}\'\n')
        self.assertEqual(self.run_watchdog(),['report','unexpected-guest','launchctl','push'])

    def test_report_failure_cannot_admit_even_if_its_partial_json_looks_healthy(self):
        self.write(self.tools/'computer-resources.sh', '#!/bin/bash\n'
                   'echo report >> "$VOS_TEST_ACTIONS"\n'
                   'echo \'{"disk_free_kib":14680064,"memory_free_percent":50,"swap_used_mib":4000,"cpu_count":10,"load_one":4}\'\nexit 1\n')
        self.assertEqual(self.run_watchdog(),['report'])
        self.assertFalse((self.root/'.watchdog').exists())


if __name__ == '__main__':
    unittest.main()
