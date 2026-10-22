#!/usr/bin/env python3
"""Resume one interrupted R2 archive, retaining its clone until shell audit."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

PART_BYTES = 8 * 1024**2
KEY_PATTERN = r"vm-backups/[0-9]{8}T[0-9]{6}Z-[0-9]+/virtualOS\.bundle\.tar\.gz"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
       "-o", "ControlMaster=no", "-o", "ControlPath=none",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
       os.environ.get("VOS_VM_SSH_HOST", "mac-mini-ai")]
CLONE = '$HOME/VirtualOSMachines/.virtualOS-r2-staging.bundle'


def clone_identity():
    command = ('test -d "' + CLONE + '" && test ! -L "' + CLONE + '" && '
               'test -f "' + CLONE + '/.complete" && '
               '! lsof "' + CLONE + '/Disk.img" >/dev/null 2>&1 && '
               'cd "' + CLONE + '" && '
               'stat -f "%N %i %z %m %c" . .complete Disk.img AuxiliaryStorage '
               'HardwareModel MachineIdentifier Parameters.txt')
    row = subprocess.run(SSH + [command], capture_output=True, text=True, timeout=30)
    if row.returncode:
        raise ValueError("complete, closed recovery clone required")
    return row.stdout


def check_pending(path):
    row = json.loads(Path(path).read_text())
    if (not isinstance(row, dict) or row.get("bucket") != "virtualos-vm-backups" or
            not isinstance(row.get("key"), str) or not re.fullmatch(KEY_PATTERN, row["key"]) or
            not isinstance(row.get("sha256"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) or
            type(row.get("bytes")) is not int or row["bytes"] < 1024 * 1024 or
            not isinstance(row.get("clone_identity"), str)):
        raise ValueError("invalid pending receipt")
    if clone_identity() != row["clone_identity"]:
        raise ValueError("pending receipt belongs to a different recovery clone")
    return row


def validate_parts(parts):
    if not isinstance(parts, list) or len(parts) > 10000:
        raise ValueError("invalid multipart part list")
    previous = 0
    for part in parts:
        if (not isinstance(part, dict) or
                type(part.get("PartNumber")) is not int or part["PartNumber"] != previous + 1
                or type(part.get("Size")) is not int or not 1 <= part["Size"] <= PART_BYTES
                or not isinstance(part.get("ETag"), str)
                or not re.fullmatch(r'"[0-9a-f]{32}"', part["ETag"])):
            raise ValueError("existing multipart parts are not a contiguous MD5-identified prefix")
        if previous and parts[previous - 1]["Size"] != PART_BYTES:
            raise ValueError("only the final existing part may be short")
        previous = part["PartNumber"]


def read_part(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def prefetched_parts(stream, part_bytes):
    """Overlap one upcoming SSH read with the current R2 upload."""
    reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vm-backup-read")
    future = reader.submit(read_part, stream, part_bytes)
    try:
        while data := future.result():
            future = reader.submit(read_part, stream, part_bytes)
            yield data
    finally:
        # On an upload failure, the caller kills the SSH source. Waiting here
        # would prevent that cleanup while an upcoming read is still blocked.
        future.cancel()
        reader.shutdown(wait=False, cancel_futures=True)


def transfer(stream, existing, put_part, check_space, part_bytes=PART_BYTES, allow_empty=False,
             part_offset=0, byte_offset=0):
    """Verify the entire old prefix before any PUT; hash both reused/new bytes."""
    validate_parts(existing)
    checksum = hashlib.sha256()
    completed = []
    size = 0
    last_check = time.monotonic()
    check_space()
    for part in existing:
        data = read_part(stream, part["Size"])
        etag = '"' + hashlib.md5(data).hexdigest() + '"'
        if len(data) != part["Size"] or etag != part["ETag"]:
            raise ValueError("archive differs from existing R2 part %s; clone and upload retained"
                             % part["PartNumber"])
        checksum.update(data)
        size += len(data)
        completed.append({"PartNumber": part["PartNumber"], "ETag": etag})
        if time.monotonic() - last_check >= 60:
            check_space()
            last_check = time.monotonic()
        if len(completed) % 64 == 0:
            print("vm_resume=verifying_prefix parts=%s bytes=%s" % (len(completed), size), flush=True)
    reused = size
    print("vm_resume=prefix_verified parts=%s bytes=%s" % (len(existing), size), flush=True)
    for data in prefetched_parts(stream, part_bytes):
        if existing and existing[-1]["Size"] < part_bytes:
            raise ValueError("archive continues after a short existing final part")
        number = part_offset + len(completed) + 1
        if number > 10000:
            raise ValueError("archive exceeds multipart part-count bound")
        if time.monotonic() - last_check >= 60:
            check_space()
            last_check = time.monotonic()
        expected = '"' + hashlib.md5(data).hexdigest() + '"'
        etag = put_part(number, data)
        if etag != expected:
            raise ValueError("newly uploaded part failed MD5 verification")
        completed.append({"PartNumber": number, "ETag": etag})
        checksum.update(data)
        size += len(data)
        print("vm_resume=uploaded part=%s compressed_bytes=%s" % (number, byte_offset + size), flush=True)
    if not completed and not allow_empty:
        raise ValueError("empty archive refused")
    return {"Parts": completed}, {"bytes": size, "sha256": checksum.hexdigest(),
                                  "reused_bytes": reused}


def atomic_json(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(row, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def resume(bucket, endpoint, receipt_path):
    account = "c24a3fb4bedba669785c18c834f51921"
    if (bucket != "virtualos-vm-backups" or
            endpoint != "https://%s.r2.cloudflarestorage.com" % account or
            os.environ.get("CLOUDFLARE_ACCOUNT_ID") != account or
            os.environ.get("CLOUDFLARE_R2_ENDPOINT") != endpoint):
        raise ValueError("resume requires the pinned private VM bucket and account")

    def r2(operation, *arguments):
        result = subprocess.run(["aws", "s3api", operation, "--bucket", bucket,
            "--endpoint-url", endpoint, "--cli-connect-timeout", "10",
            "--cli-read-timeout", "120", *arguments], capture_output=True,
            text=True, timeout=600)
        if result.returncode:
            raise RuntimeError("R2 %s failed (exit %s); multipart and clone retained"
                               % (operation, result.returncode))
        row = json.loads(result.stdout or "{}")
        if "Error" in row:
            raise RuntimeError("R2 %s returned an error" % operation)
        return row

    def check_space():
        row = subprocess.run(SSH + ["df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}'"],
                             capture_output=True, text=True, timeout=20)
        if row.returncode or not row.stdout.strip().isdigit() or int(row.stdout) < 10 * 1024**2:
            raise ValueError("Mini unavailable or disk below 10 GiB; clone and upload retained")

    uploads = [row for row in r2("list-multipart-uploads").get("Uploads", [])
               if re.fullmatch(KEY_PATTERN, row.get("Key", ""))]
    if len(uploads) != 1:
        raise ValueError("resume requires exactly one interrupted VM archive")
    upload = uploads[0]
    key, upload_id = upload["Key"], upload["UploadId"]
    arguments = ["--key", key, "--upload-id", upload_id]
    parts = r2("list-parts", *arguments).get("Parts", [])
    validate_parts(parts)
    initial = clone_identity()
    check_space()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vos-vm-resume-") as scratch:
        part_path = Path(scratch) / "part.bin"

        def put_part(number, data):
            part_path.write_bytes(data)
            return r2("upload-part", *arguments, "--part-number", str(number),
                      "--body", str(part_path))["ETag"]

        # stderr uses a file so tar/ssh diagnostics cannot block the archive pipe.
        with tempfile.TemporaryFile() as errors:
            filter_code = Path(__file__).with_name("vm_backup_source.py").read_text()
            # fd 3 carries metadata independently of gzip's pipe on stdin.
            # Keeping the part list out of argv avoids macOS ARG_MAX failures
            # when a long archive has thousands of already uploaded parts.
            pipeline = ('exec 3<&0; tar -cf - -C "' + CLONE + '" . | gzip -1 -n | python3 -c '
                        + shlex.quote(filter_code) + " --parts-fd=3")
            source = subprocess.Popen(SSH + ["bash -o pipefail -c " + shlex.quote(pipeline)],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
            try:
                source.stdin.write((json.dumps(parts) + "\n").encode())
                source.stdin.close()
                header = source.stdout.readline(4096)
                if not header.endswith(b"\n"):
                    errors.seek(0)
                    detail = errors.read().decode(errors="replace")[-500:].strip()
                    raise ValueError("Mini prefix verification failed; no new parts uploaded: " + detail)
                header = json.loads(header)
                reused = sum(part["Size"] for part in parts)
                if header != {"prefix_parts": len(parts), "prefix_bytes": reused}:
                    raise ValueError("invalid Mini prefix verification response")
                print("vm_resume=prefix_verified_on_mini parts=%s bytes=%s" % (len(parts), reused), flush=True)
                tail_completed, tail_receipt = transfer(
                    source.stdout, [], put_part, check_space, allow_empty=True,
                    part_offset=len(parts), byte_offset=reused)
                if source.wait(timeout=30):
                    raise ValueError("archive source failed; multipart and clone retained")
                errors.seek(0)
                source_rows = [line[len("vm_resume_source="):] for line in
                               errors.read().decode(errors="replace").splitlines()
                               if line.startswith("vm_resume_source=")]
                if len(source_rows) != 1:
                    raise ValueError("Mini did not return a full archive hash")
                receipt = json.loads(source_rows[0])
                if (type(receipt.get("bytes")) is not int or receipt["bytes"] < 1024 * 1024 or
                        receipt["bytes"] != reused + tail_receipt["bytes"] or
                        receipt.get("reused_bytes") != reused or
                        not isinstance(receipt.get("sha256"), str) or
                        not re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]) or
                        receipt.get("tail_sha256") != tail_receipt["sha256"]):
                    raise ValueError("Mini archive hash or tail verification failed")
                completed = {"Parts": [{"PartNumber": part["PartNumber"], "ETag": part["ETag"]}
                                       for part in parts] + tail_completed["Parts"]}
                if clone_identity() != initial:
                    raise ValueError("recovery clone changed during transfer")
            finally:
                if source.poll() is None:
                    source.kill()
                source.wait(timeout=30)
                source.stdout.close()
                source.stdin.close()
        receipt.update(bucket=bucket, key=key, clone_identity=initial,
                       upload_seconds=round(time.monotonic() - started, 1))
        # Persist the full-stream hash before committing. If completion succeeds
        # but the caller exits, the shell can recover this candidate and audit it.
        atomic_json(receipt_path, receipt)
        completion_path = Path(scratch) / "parts.json"
        completion_path.write_text(json.dumps(completed))
        r2("complete-multipart-upload", *arguments, "--multipart-upload", "file://" + str(completion_path))
        print("vm_resume=completed bytes=%s sha256=%s" % (receipt["bytes"], receipt["sha256"]), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check-pending":
        try:
            row = check_pending(sys.argv[2])
            print(row["key"], row["bytes"])
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            raise SystemExit("vm_resume=failed: %s" % error) from None
        raise SystemExit(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bucket")
    parser.add_argument("endpoint")
    parser.add_argument("pending_receipt")
    args = parser.parse_args()
    def interrupted(*_):
        raise InterruptedError("transfer interrupted; multipart and clone retained")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        lock_path = Path(args.pending_receipt + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            resume(args.bucket, args.endpoint, args.pending_receipt)
        finally:
            os.close(lock_fd)
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise SystemExit("vm_resume=failed: %s" % error) from None
