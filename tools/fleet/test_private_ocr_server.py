import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import private_ocr_server as private


class PrivateOCRTests(unittest.TestCase):
    def test_log_links_cannot_truncate_an_existing_file(self):
        for linked in ('symlink','hardlink','fifo'):
            with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
                root=Path(name); target=root/'preserved'; target.write_bytes(b'unchanged'); target.chmod(0o600)
                log=root/'log'
                if linked=='symlink': log.symlink_to(target)
                elif linked=='hardlink': os.link(target,log)
                else: os.mkfifo(log,0o600)
                with self.assertRaises((OSError,private.PrivateOCRError)):
                    private.private_output(log)
                self.assertEqual(target.read_bytes(),b'unchanged')

    def test_native_sandbox_allows_loopback_but_denies_external_network_and_outside_write(self):
        with tempfile.TemporaryDirectory(prefix='vos-ocr-sandbox-',dir='/private/tmp') as name:
            stage=Path(name)
            env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','USER','LOGNAME','LANG')}
            private.check_sandbox(stage,private.sandbox_profile(stage),env)

    def test_unverified_stage_never_starts_a_process(self):
        with patch.object(private,'verify_stage',side_effect=ValueError('unverified')), \
             patch.object(private.subprocess,'Popen') as launch:
            with self.assertRaises(ValueError):
                with private.serve(Path('/unverified'),Path('/guard')):
                    self.fail('unverified backend became usable')
        launch.assert_not_called()

    def test_resource_guard_refusal_prevents_global_probe_and_process_start(self):
        with patch.object(private,'verify_stage',return_value=(Path('/private'),400*1024**2)), \
             patch.object(private.subprocess,'run',return_value=Mock(returncode=75)), \
             patch.object(private.pinned,'metadata') as global_probe, \
             patch.object(private.subprocess,'Popen') as launch:
            with self.assertRaises(private.PrivateOCRError):
                with private.serve(Path('/private'),Path('/guard')): pass
        global_probe.assert_not_called()
        launch.assert_not_called()

    def test_pressure_and_bad_metrics_interrupt_the_private_session(self):
        good=dict(available_bytes=5*private.GIB,disk_free_bytes=12*private.GIB,
                  cpus=8,load=3.,swap_used_bytes=private.GIB,swapouts=12)
        self.assertIsNone(private.pressure_reason(good,12))
        for update in ({'available_bytes':3*private.GIB},{'disk_free_bytes':9*private.GIB},
                       {'swapouts':13},{'swapouts':11},{'load':float('nan')},
                       {'available_bytes':True},{'swap_used_bytes':8*private.GIB}):
            self.assertIsNotNone(private.pressure_reason(dict(good,**update),12))

    def test_existing_global_model_prevents_private_process_start(self):
        with patch.object(private,'verify_stage',return_value=(Path('/private'),400*1024**2)), \
             patch.object(private.subprocess,'run',return_value=Mock(returncode=0)), \
             patch.object(private.pinned,'metadata',return_value={'models':[{'name':'existing'}]}), \
             patch.object(private,'resources') as probe, \
             patch.object(private.subprocess,'Popen') as launch:
            with self.assertRaises(private.PrivateOCRError):
                with private.serve(Path('/private'),Path('/guard')): pass
        probe.assert_not_called()
        launch.assert_not_called()

    def test_reaped_child_is_never_signalled_by_its_reusable_pid(self):
        child=Mock(pid=12345,returncode=0)
        with patch.object(private.os,'killpg') as send:
            private.stop_owned_group(child)
        send.assert_not_called()
        child.wait.assert_called_once_with(timeout=5)

    def test_global_model_recheck_refuses_a_later_foreign_load(self):
        with patch.object(private.pinned,'metadata',side_effect=[{'models':[]},{'models':[{'name':'foreign'}]}]) as probe:
            private.require_global_idle()
            with self.assertRaises(private.PrivateOCRError):
                private.require_global_idle()
        self.assertEqual(probe.call_count,2)
        self.assertEqual(probe.call_args.args,(11434,'/api/ps'))

    def test_late_pressure_probe_never_signals_after_cleanup(self):
        child=Mock(pid=12345,returncode=None)
        stop=threading.Event(); lock=threading.Lock()
        monitor=Mock()
        monitor.is_alive.return_value=True
        with patch.object(private,'stop_owned_group') as cleanup, \
             patch.object(private,'terminate_owned_group') as terminate:
            with self.assertRaises(private.PrivateOCRError):
                private.cleanup_owned_session(child,stop,lock,monitor)
            cleanup.assert_called_once_with(child)
            private.interrupt_owned_group(child,stop,lock)
            terminate.assert_not_called()
        self.assertTrue(stop.is_set())

    def test_pressure_kills_descendants_of_exited_unreaped_startup_leader(self):
        runner='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(30)'
        code=('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",'
              +repr(runner)+'],stdout=subprocess.PIPE,text=True);p.stdout.readline();print(p.pid,flush=True)')
        child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,text=True,start_new_session=True)
        try:
            runner_pid=int(child.stdout.readline())
            time.sleep(.15)  # Leader exits, deliberately remains unreaped.
            self.assertIsNone(child.returncode)
            stop=threading.Event(); lock=threading.Lock()
            private.interrupt_owned_group(child,stop,lock)
            self.assertIsNone(child.returncode)
            result=subprocess.run(['ps','-p',str(runner_pid),'-o','stat='],capture_output=True,text=True,timeout=5)
            self.assertTrue(result.returncode!=0 or result.stdout.strip().startswith('Z'))
        finally:
            private.stop_owned_group(child)
            child.stdout.close()

    def test_cleanup_kills_own_runner_group_while_unrelated_process_survives(self):
        sentinel=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        self.addCleanup(lambda: (sentinel.terminate(),sentinel.wait(timeout=5)) if sentinel.poll() is None else None)
        # Runner ignores TERM, so cleanup must clear descendants before reaping
        # the leader. This uses no VM, model, network or external process ID.
        runner='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(30)'
        code=('import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",'
              +repr(runner)+'],stdout=subprocess.PIPE,text=True);p.stdout.readline();print(p.pid,flush=True);time.sleep(30)')
        child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,text=True,start_new_session=True)
        try:
            runner_pid=int(child.stdout.readline())
            private.stop_owned_group(child)
            self.assertIsNotNone(child.returncode)
            self.assertIsNone(sentinel.poll())
            # A briefly unreaped zombie has already released its model memory.
            result=subprocess.run(['ps','-p',str(runner_pid),'-o','stat='],capture_output=True,text=True,timeout=5)
            self.assertTrue(result.returncode!=0 or result.stdout.strip().startswith('Z'))
        finally:
            if child.returncode is None:
                private.stop_owned_group(child)
            child.stdout.close()


if __name__=='__main__':
    unittest.main()
