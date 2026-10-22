from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vm_backup_recovery import probe, retry_allowed


class RecoveryTest(unittest.TestCase):
    def test_ready_requires_complete_clone_and_disk_reserve(self):
        row = SimpleNamespace(returncode=0, stdout="disk_free_kib=33554432\nclone=complete\n")
        with patch("vm_backup_recovery.subprocess.run", return_value=row) as run:
            self.assertEqual(probe("mini-alternate")["state"], "ready")
            self.assertEqual(run.call_args.args[0][-2], "mini-alternate")
            self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_offline_and_timeout_are_transient(self):
        with patch("vm_backup_recovery.subprocess.run", return_value=SimpleNamespace(returncode=255)):
            self.assertEqual(probe("mac-mini-ai")["state"], "offline")
        with patch("vm_backup_recovery.subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 20)):
            self.assertEqual(probe("mac-mini-ai")["reason"], "ssh_timeout")

    def test_low_disk_and_missing_clone_cannot_start_upload(self):
        for output, expected in (("disk_free_kib=10485759\nclone=complete\n", "low_disk"),
                                 ("disk_free_kib=33554432\nclone=unavailable\n", "clone_unavailable"),
                                 ("disk_free_kib=unknown\nclone=complete\n", "unavailable")):
            with self.subTest(expected=expected), patch("vm_backup_recovery.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout=output)):
                self.assertEqual(probe("mac-mini-ai")["state"], expected)

    def test_stale_clone_reader_defers_restart(self):
        with patch("vm_backup_recovery.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="disk_free_kib=33554432\nclone=busy\n")):
            row = probe("mac-mini-ai")
            self.assertEqual(row["state"], "busy")
            self.assertEqual(row["reason"], "clone_reader_still_active")

    def test_host_cannot_be_an_ssh_option(self):
        with patch("vm_backup_recovery.subprocess.run", side_effect=AssertionError("unexpected SSH")):
            with self.assertRaises(ValueError):
                probe("-oProxyCommand=anything")

    def test_connection_failure_can_retry_but_integrity_failure_cannot(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "backup.log"
            for failure, allowed in (
                    ("Mini unavailable or disk below 10 GiB; clone and upload retained", True),
                    ("R2 upload-part failed (exit 255); multipart and clone retained", True),
                    ("archive differs from existing R2 part 3", False),
                    ("Mini archive hash or tail verification failed", False),
                    ("newly uploaded part failed MD5 verification", False),
                    ("recovery clone changed during transfer", False),
                    ("pending receipt belongs to a different recovery clone", False),
                    ("Mini prefix verification failed; no new parts uploaded: Connection reset", True),
                    ("Mini prefix verification failed; no new parts uploaded: tar: read error", False)):
                with self.subTest(failure=failure):
                    log.write_text("vm_resume=failed: " + failure + "\nvm_halt_preserve=complete\n")
                    self.assertEqual(retry_allowed(log), allowed)

    def test_latest_failure_controls_retry(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "backup.log"
            log.write_text("vm_resume=failed: archive differs\nvm_resume=failed: Mini unavailable\n")
            self.assertTrue(retry_allowed(log))
            log.write_text("vm_resume=failed: Mini unavailable\nvm_resume=failed: archive differs\n")
            self.assertFalse(retry_allowed(log))


if __name__ == "__main__":
    unittest.main()
