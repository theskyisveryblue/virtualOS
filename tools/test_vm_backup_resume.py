import hashlib
import io
import json
import shlex
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vm_backup_resume import PART_BYTES, atomic_json, check_pending, resume, transfer, validate_parts
from vm_backup_source import forward


def part(number, data):
    return {"PartNumber": number, "Size": len(data),
            "ETag": '"' + hashlib.md5(data).hexdigest() + '"'}


class ResumeTest(unittest.TestCase):
    def test_resume_reuses_prefix_and_hashes_whole_archive(self):
        first = b"a" * PART_BYTES
        tail = b"remaining archive bytes"
        writes = []

        def put(number, data):
            writes.append((number, data))
            return part(number, data)["ETag"]

        completed, receipt = transfer(io.BytesIO(first + tail), [part(1, first)], put, lambda: None)
        self.assertEqual(writes, [(2, tail)])
        self.assertEqual([p["PartNumber"] for p in completed["Parts"]], [1, 2])
        self.assertEqual(receipt["reused_bytes"], len(first))
        self.assertEqual(receipt["bytes"], len(first + tail))
        self.assertEqual(receipt["sha256"], hashlib.sha256(first + tail).hexdigest())

    def test_later_prefix_mismatch_makes_no_remote_writes(self):
        first = b"a" * PART_BYTES
        second = b"b" * PART_BYTES
        with self.assertRaisesRegex(ValueError, "differs.*part 2"):
            transfer(io.BytesIO(first + b"c" * PART_BYTES),
                     [part(1, first), part(2, second)],
                     lambda *_: self.fail("PUT before verifying entire prefix"), lambda: None)

    def test_truncated_source_preserves_upload(self):
        with self.assertRaisesRegex(ValueError, "differs"):
            transfer(io.BytesIO(b"short"), [part(1, b"full archive")],
                     lambda *_: self.fail("unexpected PUT"), lambda: None)

    def test_completed_short_part_needs_no_upload(self):
        data = b"complete archive"
        completed, receipt = transfer(io.BytesIO(data), [part(1, data)],
                                     lambda *_: self.fail("unexpected PUT"), lambda: None)
        self.assertEqual(len(completed["Parts"]), 1)
        self.assertEqual(receipt["reused_bytes"], len(data))

    def test_short_existing_part_cannot_be_followed_by_more_data(self):
        with self.assertRaisesRegex(ValueError, "continues"):
            transfer(io.BytesIO(b"oldnew"), [part(1, b"old")],
                     lambda *_: self.fail("unexpected PUT"), lambda: None)

    def test_invalid_existing_metadata_is_rejected(self):
        for rows in ([part(2, b"x")], [part(1, b"x"), part(2, b"y")],
                     [{"PartNumber": True, "Size": 1, "ETag": part(1, b"x")["ETag"]}],
                     [{"PartNumber": 1, "Size": 1, "ETag": '"opaque-etag"'}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate_parts(rows)

    def test_failed_uploaded_part_hash_never_returns_completion(self):
        with self.assertRaisesRegex(ValueError, "MD5"):
            transfer(io.BytesIO(b"new archive"), [], lambda *_: '"' + "0" * 32 + '"', lambda: None)

    def test_resource_guard_prevents_remote_write(self):
        def deny():
            raise ValueError("low disk")

        with self.assertRaisesRegex(ValueError, "low disk"):
            transfer(io.BytesIO(b"data"), [], lambda *_: self.fail("unexpected PUT"), deny)

    def test_interrupted_transfer_can_resume_the_accepted_new_part(self):
        first = b"a" * PART_BYTES
        archive = first + b"tail"
        accepted = []

        def interrupt(number, data):
            if number == 2:
                raise OSError("connection lost")
            accepted.append(part(number, data))
            return accepted[-1]["ETag"]

        with self.assertRaises(OSError):
            transfer(io.BytesIO(archive), [], interrupt, lambda: None)
        writes = []

        def put(number, data):
            writes.append(number)
            return part(number, data)["ETag"]

        _, receipt = transfer(io.BytesIO(archive), accepted, put, lambda: None)
        self.assertEqual(writes, [2])
        self.assertEqual(receipt["sha256"], hashlib.sha256(archive).hexdigest())

    def test_candidate_receipt_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "pending.json"
            path.write_text("previous candidate")
            atomic_json(path, {"bytes": 123})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(), '{"bytes": 123}\n')
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_mini_filter_only_sends_tail_and_hashes_entire_source(self):
        prefix = b"a" * PART_BYTES
        tail = b"new archive bytes"
        output, diagnostics = io.BytesIO(), io.StringIO()
        forward(io.BytesIO(prefix + tail), output, diagnostics, [part(1, prefix)])
        header, forwarded = output.getvalue().split(b"\n", 1)
        self.assertEqual(json.loads(header), {"prefix_parts": 1, "prefix_bytes": len(prefix)})
        self.assertEqual(forwarded, tail)
        receipt = json.loads(diagnostics.getvalue().removeprefix("vm_resume_source="))
        self.assertEqual(receipt["sha256"], hashlib.sha256(prefix + tail).hexdigest())
        self.assertEqual(receipt["tail_sha256"], hashlib.sha256(tail).hexdigest())
        writes = []

        def put(number, data):
            writes.append(number)
            return part(number, data)["ETag"]

        completed, _ = transfer(io.BytesIO(forwarded), [], put, lambda: None,
                                part_offset=1, byte_offset=len(prefix), allow_empty=True)
        self.assertEqual(writes, [2])
        self.assertEqual(completed["Parts"][0]["PartNumber"], 2)

    def test_mini_prefix_mismatch_emits_no_header_or_tail(self):
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "differs"):
            forward(io.BytesIO(b"wrong"), output, io.StringIO(), [part(1, b"right")])
        self.assertEqual(output.getvalue(), b"")

    def test_mini_filter_short_last_part_refuses_continuation(self):
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "continues"):
            forward(io.BytesIO(b"oldnew"), output, io.StringIO(), [part(1, b"old")])
        self.assertEqual(output.getvalue(), b"")

    def test_mini_filter_handles_all_bytes_already_uploaded(self):
        output, diagnostics = io.BytesIO(), io.StringIO()
        forward(io.BytesIO(b"archive"), output, diagnostics, [part(1, b"archive")])
        header, tail = output.getvalue().split(b"\n", 1)
        self.assertEqual(tail, b"")
        self.assertEqual(json.loads(header)["prefix_bytes"], 7)
        completed, receipt = transfer(io.BytesIO(tail), [],
                                      lambda *_: self.fail("unexpected PUT"), lambda: None,
                                      allow_empty=True)
        self.assertEqual(completed["Parts"], [])
        self.assertEqual(receipt["sha256"], hashlib.sha256(b"").hexdigest())

    def test_pending_receipt_requires_the_same_preserved_clone(self):
        row = {"bucket": "virtualos-vm-backups",
               "key": "vm-backups/20261011T230151Z-91132/virtualOS.bundle.tar.gz",
               "sha256": "a" * 64, "bytes": 2 * 1024**2,
               "clone_identity": "original clone"}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "pending.json"
            atomic_json(path, row)
            with patch("vm_backup_resume.clone_identity", return_value="original clone"):
                self.assertEqual(check_pending(path), row)
            with patch("vm_backup_resume.clone_identity", return_value="replacement clone"):
                with self.assertRaisesRegex(ValueError, "different recovery clone"):
                    check_pending(path)

    def test_pending_receipt_without_clone_identity_is_rejected_before_ssh(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "pending.json"
            atomic_json(path, {"bucket": "virtualos-vm-backups", "bytes": 123})
            with patch("vm_backup_resume.clone_identity", side_effect=AssertionError("unexpected SSH")):
                with self.assertRaisesRegex(ValueError, "invalid pending receipt"):
                    check_pending(path)

    def test_completion_persists_candidate_and_preserves_it_on_commit_failure(self):
        account = "c24a3fb4bedba669785c18c834f51921"
        endpoint = "https://%s.r2.cloudflarestorage.com" % account
        key = "vm-backups/20261011T230151Z-91132/virtualOS.bundle.tar.gz"
        prefix, tail = b"a" * PART_BYTES, b"b" * 1024**2
        existing = [part(1, prefix)]
        for fail_commit in (False, True):
            with self.subTest(fail_commit=fail_commit), tempfile.TemporaryDirectory() as root:
                candidate = Path(root) / "pending.json"
                calls = []

                def run(command, **_):
                    if command[0] == "ssh":
                        return SimpleNamespace(returncode=0, stdout=str(33 * 1024**2))
                    operation = command[2]
                    calls.append(operation)
                    response = {}
                    if operation == "list-multipart-uploads":
                        response = {"Uploads": [{"Key": key, "UploadId": "fixture-upload"}]}
                    elif operation == "list-parts":
                        response = {"Parts": existing}
                    elif operation == "upload-part":
                        number = int(command[command.index("--part-number") + 1])
                        data = Path(command[command.index("--body") + 1]).read_bytes()
                        self.assertEqual(number, 2)
                        self.assertEqual(data, tail)
                        response = {"ETag": part(number, data)["ETag"]}
                    elif operation == "complete-multipart-upload":
                        # The durable candidate must exist before the commit call.
                        self.assertTrue(candidate.exists())
                        filename = command[command.index("--multipart-upload") + 1].removeprefix("file://")
                        completion = json.loads(Path(filename).read_text())
                        self.assertEqual([row["PartNumber"] for row in completion["Parts"]], [1, 2])
                        if fail_commit:
                            return SimpleNamespace(returncode=1, stdout="")
                    return SimpleNamespace(returncode=0, stdout=json.dumps(response))

                def start_source(_command, **kwargs):
                    self.assertLess(len(_command[-1]), 8192)
                    self.assertIn("--parts-fd=3", _command[-1])
                    output, diagnostics = io.BytesIO(), io.StringIO()
                    forward(io.BytesIO(prefix + tail), output, diagnostics, existing)
                    kwargs["stderr"].write(diagnostics.getvalue().encode())
                    kwargs["stderr"].flush()
                    output.seek(0)
                    return SimpleNamespace(stdin=io.BytesIO(), stdout=output,
                                           wait=lambda **_: 0, poll=lambda: 0)

                with patch.dict("os.environ", {"CLOUDFLARE_ACCOUNT_ID": account,
                                               "CLOUDFLARE_R2_ENDPOINT": endpoint}), \
                     patch("vm_backup_resume.clone_identity", return_value="same clone"), \
                     patch("vm_backup_resume.subprocess.run", side_effect=run), \
                     patch("vm_backup_resume.subprocess.Popen", side_effect=start_source):
                    if fail_commit:
                        with self.assertRaisesRegex(RuntimeError, "complete-multipart-upload failed"):
                            resume("virtualos-vm-backups", endpoint, candidate)
                    else:
                        resume("virtualos-vm-backups", endpoint, candidate)
                row = json.loads(candidate.read_text())
                self.assertEqual(row["sha256"], hashlib.sha256(prefix + tail).hexdigest())
                self.assertEqual(row["clone_identity"], "same clone")
                self.assertEqual(row["reused_bytes"], len(prefix))
                self.assertNotIn("abort-multipart-upload", calls)
                self.assertEqual(calls.count("upload-part"), 1)

    def test_real_shell_pipe_keeps_part_metadata_separate_from_archive_bytes(self):
        prefix, tail = b"a" * PART_BYTES, b"remaining archive"
        code = Path(__file__).with_name("vm_backup_source.py").read_text()
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / "archive.bin"
            archive.write_bytes(prefix + tail)
            command = ("exec 3<&0; cat " + shlex.quote(str(archive)) +
                       " | " + shlex.quote(sys.executable) + " -c " + shlex.quote(code) + " --parts-fd=3")
            result = subprocess.run(["bash", "-o", "pipefail", "-c", command],
                                    input=(json.dumps([part(1, prefix)]) + "\n").encode(),
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            header, forwarded = result.stdout.split(b"\n", 1)
            self.assertEqual(json.loads(header)["prefix_bytes"], len(prefix))
            self.assertEqual(forwarded, tail)
            row = json.loads(result.stderr.decode().removeprefix("vm_resume_source="))
            self.assertEqual(row["sha256"], hashlib.sha256(prefix + tail).hexdigest())

            # A multi-MiB list is larger than macOS permits in a command. It must
            # reach the filter via fd 3 and fail only on the synthetic source's
            # missing second block, without forwarding unverified bytes.
            prototype = part(1, prefix)
            parts = [dict(prototype, PartNumber=number, fixture_metadata="x" * 400)
                     for number in range(1, 5001)]
            payload = (json.dumps(parts) + "\n").encode()
            self.assertGreater(len(payload), 1024**2)
            result = subprocess.run(["bash", "-o", "pipefail", "-c", command],
                                    input=payload, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"archive differs from existing R2 part 2", result.stderr)
            self.assertEqual(result.stdout, b"")

    def test_next_part_is_read_while_current_part_uploads(self):
        second_read = threading.Event()
        archive = b"a" * 16 + b"b" * 16 + b"tail"

        class ObservedStream(io.BytesIO):
            def read(self, size):
                if self.tell() >= 16:
                    second_read.set()
                return super().read(size)

        def put(number, data):
            if number == 1:
                self.assertTrue(second_read.wait(timeout=2), "next read did not overlap upload")
            return part(number, data)["ETag"]

        completed, receipt = transfer(ObservedStream(archive), [], put, lambda: None, part_bytes=16)
        self.assertEqual([row["PartNumber"] for row in completed["Parts"]], [1, 2, 3])
        self.assertEqual(receipt["sha256"], hashlib.sha256(archive).hexdigest())

    def test_upload_failure_does_not_wait_for_a_blocked_prefetch_read(self):
        reading = threading.Event()
        release = threading.Event()

        class BlockingStream(io.BytesIO):
            def read(self, size):
                if self.tell() >= 16:
                    reading.set()
                    release.wait(timeout=3)
                return super().read(size)

        def fail_upload(*_):
            self.assertTrue(reading.wait(timeout=1))
            raise OSError("R2 upload failed")

        started = time.monotonic()
        try:
            with self.assertRaisesRegex(OSError, "R2 upload failed"):
                transfer(BlockingStream(b"a" * 32), [], fail_upload, lambda: None, part_bytes=16)
            self.assertLess(time.monotonic() - started, 1,
                            "prefetch blocked the caller's source cleanup")
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
