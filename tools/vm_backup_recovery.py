"""Read-only gates for the backup monitor's connection recovery."""

import json
from pathlib import Path
import re
import subprocess
import sys


def probe(host):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}", host):
        raise ValueError("invalid SSH host")
    command = ("df -Pk /System/Volumes/Data | awk 'NR==2 {print \"disk_free_kib=\" $4}'; "
               'clone="$HOME/VirtualOSMachines/.virtualOS-r2-staging.bundle"; '
               'if test -d "$clone" && test ! -L "$clone" && '
               'test -f "$clone/.complete" && test -f "$clone/Disk.img"; '
               'then if lsof "$clone/Disk.img" >/dev/null 2>&1; '
               'then echo clone=busy; else echo clone=complete; fi; '
               'else echo clone=unavailable; fi')
    try:
        result = subprocess.run([
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", host, command
        ], capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return {"state": "offline", "reason": "ssh_timeout"}
    if result.returncode:
        return {"state": "offline", "reason": "ssh_failed"}
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    free = fields.get("disk_free_kib", "")
    if not free.isdigit():
        return {"state": "unavailable", "reason": "invalid_resource_response"}
    free = int(free)
    if free < 10 * 1024**2:
        return {"state": "low_disk", "disk_free_kib": free}
    if fields.get("clone") == "busy":
        return {"state": "busy", "reason": "clone_reader_still_active", "disk_free_kib": free}
    if fields.get("clone") != "complete":
        return {"state": "clone_unavailable", "disk_free_kib": free}
    return {"state": "ready", "disk_free_kib": free}


def retry_allowed(log_path):
    path = Path(log_path)
    if not path.exists():
        return True
    # Read only a bounded tail, and retain the latest explicit uploader failure
    # even if a later stop operation appends another line.
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 16384))
        lines = stream.read().decode(errors="replace").splitlines()
    failures = [line for line in lines if line.startswith("vm_resume=failed:")]
    if not failures:
        return True
    failure = failures[-1]
    integrity_errors = ("archive differs", "hash", "MD5", "changed during transfer",
                        "different recovery clone", "invalid pending receipt",
                        "invalid multipart", "contiguous MD5", "invalid Mini prefix",
                        "archive continues", "complete, closed recovery clone required")
    if any(message in failure for message in integrity_errors):
        return False
    # A prefix verification failure is retryable only when SSH itself failed.
    if "Mini prefix verification failed" in failure:
        return any(message in failure.lower() for message in
                   ("timed out", "connection reset", "connection closed", "no route to host",
                    "could not resolve hostname", "network is unreachable", "broken pipe"))
    return True


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "probe":
        try:
            row = probe(sys.argv[2])
        except (ValueError, OSError) as error:
            raise SystemExit(str(error)) from None
        print(json.dumps(row, sort_keys=True))
        raise SystemExit({"ready": 0, "offline": 75, "busy": 75}.get(row["state"], 76))
    if len(sys.argv) == 3 and sys.argv[1] == "can-retry":
        allowed = retry_allowed(sys.argv[2])
        print("vm_recovery=retry_allowed" if allowed else "vm_recovery=integrity_refusal")
        raise SystemExit(0 if allowed else 1)
    raise SystemExit("usage: vm_backup_recovery.py probe HOST|can-retry LOG")
