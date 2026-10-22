"""Exercise the existing boot shell with scratch bundles and stubbed services."""
import json
from contextlib import closing
import os
from pathlib import Path
import subprocess
import signal
import socket
import sqlite3
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("go-live.sh")


class GoLiveTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="vos-go-live-", dir="/private/tmp")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.fleet = self.root / "fleet"
        self.bundle = self.fleet / "vm-a.bundle"
        self.bundle.mkdir(parents=True)
        for name in ("HardwareModel", "MachineIdentifier", "AuxiliaryStorage"):
            (self.bundle / name).write_bytes(("keep-" + name).encode())
        self.env = dict(os.environ, VOS_FLEET_HOME=str(self.fleet),
                        VOS_PROD_BUNDLE=str(self.root / "production.bundle"),
                        VOS_RELAY_ENV=str(self.root / "missing-relay.env"),
                        VOS_FLEET_TOOLS=str(self.root / "tools"))
        self.env.pop("VOS_ALLOW_LOCAL_CLONE", None)
        self.configure({"nbdURL": "nbd://127.0.0.1:40990/virtualos-test"})

    def configure(self, params):
        (self.bundle / "Parameters.txt").write_text(json.dumps(params))

    def contents(self):
        return {p.name: p.read_bytes() for p in self.bundle.iterdir()}

    def run_shell(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_diskless_bundle_is_preserved_on_repeated_prepare(self):
        before = self.contents()
        for _ in range(2):
            result = self.run_shell("vm-a", "--prepare-only")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.contents(), before)
            self.assertEqual((self.fleet / "machines/vm-a.bundle").resolve(), self.bundle.resolve())
        self.assertFalse((self.bundle / "Disk.img").exists())

    def test_local_disk_bundle_is_preserved(self):
        self.configure({})
        (self.bundle / "Disk.img").write_bytes(b"guest-disk")
        before = self.contents()
        self.assertEqual(self.run_shell("--prepare-only", "vm-a").returncode, 0)
        self.assertEqual(self.contents(), before)

    def test_cold_cloud_machine_refuses_automatic_local_clone(self):
        production = Path(self.env["VOS_PROD_BUNDLE"])
        self.bundle.rename(production)
        (production / "Disk.img").write_bytes(b"production")
        catalog = self.fleet / "cloud-machines"
        catalog.mkdir()
        (catalog / "vm-a.json").write_text('{"state":"cold"}')
        result = self.run_shell("vm-a", "--prepare-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stored cold in R2", result.stderr)
        self.assertFalse(self.bundle.exists())
        self.assertEqual((production / "Disk.img").read_bytes(), b"production")

    def test_invalid_existing_bundles_are_refused_without_replacement(self):
        production = Path(self.env["VOS_PROD_BUNDLE"])
        production.mkdir()
        (production / "Disk.img").write_bytes(b"production")
        for params in ({}, [], {"nbdURL": "invalid"}, {"nbdURL": ""},
                       {"nbdURL": 123}, {"nbdURL": "nbd://localhost:99999/disk"}):
            with self.subTest(params=params):
                self.configure(params)
                before = self.contents()
                self.assertNotEqual(self.run_shell("vm-a", "--prepare-only").returncode, 0)
                self.assertEqual(self.contents(), before)

    def test_missing_platform_is_refused(self):
        (self.bundle / "HardwareModel").unlink()
        before = self.contents()
        self.assertNotEqual(self.run_shell("vm-a", "--prepare-only").returncode, 0)
        self.assertEqual(self.contents(), before)

    def test_malformed_parameters_are_preserved(self):
        (self.bundle / "Parameters.txt").write_text("{broken")
        before = self.contents()
        self.assertNotEqual(self.run_shell("vm-a", "--prepare-only").returncode, 0)
        self.assertEqual(self.contents(), before)

    def test_invalid_machine_name_is_refused(self):
        self.assertNotEqual(self.run_shell("../escape", "--prepare-only").returncode, 0)
        self.assertFalse((self.root / "escape.bundle").exists())

    def test_missing_bundle_refuses_implicit_full_local_clone(self):
        production = Path(self.env["VOS_PROD_BUNDLE"])
        self.bundle.rename(production)
        (production / "Disk.img").write_bytes(b"production")
        before = {p.name: p.read_bytes() for p in production.iterdir()}
        for value in (None, "0", "true"):
            with self.subTest(value=value):
                if value is not None:
                    self.env["VOS_ALLOW_LOCAL_CLONE"] = value
                result = self.run_shell("vm-a", "--prepare-only")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("new machines require shared storage", result.stderr)
                self.assertFalse(self.bundle.exists())
                self.assertEqual({p.name: p.read_bytes() for p in production.iterdir()}, before)

    def test_explicit_local_clone_preserves_source_and_drops_nbd_setting(self):
        self.env["VOS_ALLOW_LOCAL_CLONE"] = "1"
        production = Path(self.env["VOS_PROD_BUNDLE"])
        self.bundle.rename(production)
        (production / "Disk.img").write_bytes(b"production")
        before = {p.name: p.read_bytes() for p in production.iterdir()}
        result = self.run_shell("vm-a", "--prepare-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.bundle / "Disk.img").read_bytes(), b"production")
        self.assertNotIn("nbdURL", json.loads((self.bundle / "Parameters.txt").read_text()))
        self.assertEqual({p.name: p.read_bytes() for p in production.iterdir()}, before)

    def test_existing_nondirectory_is_preserved(self):
        self.bundle.rename(self.root / "saved.bundle")
        self.bundle.write_bytes(b"keep-me")
        result = self.run_shell("vm-a", "--prepare-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.bundle.read_bytes(), b"keep-me")

    def stub_services(self, state, start_fails=False):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        status = json.dumps({"instances": [{"name": "vm-b", "state": "running"},
                                          {"name": "vm-a", "state": state}]})
        curl = bin_dir / "curl"
        curl.write_text("#!/usr/bin/env python3\nprint(" + repr(status) + ")\n")
        curl.chmod(0o755)
        launchctl = bin_dir / "launchctl"
        launchctl.write_text("#!/bin/sh\nexit 0\n")
        launchctl.chmod(0o755)
        fleet_tools = self.root / "tools/fleet"
        fleet_tools.mkdir(parents=True, exist_ok=True)
        (fleet_tools / "runbook.sh").write_text(
            '#!/bin/bash\nprintf "%s %s\\n" "$1" "$2" >> "$VOS_TEST_CALLS"\n'
            + ("exit 7\n" if start_fails else "exit 0\n"))
        self.env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
        self.env["VOS_TEST_CALLS"] = str(self.root / "calls")

    def test_other_running_vm_does_not_skip_requested_start(self):
        self.stub_services("idle")
        result = self.run_shell("vm-a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "calls").read_text(), "start vm-a\n")

    def test_requested_active_vm_is_not_started_twice(self):
        for state in ("running", "starting", "pending"):
            with self.subTest(state=state):
                self.stub_services(state)
                result = self.run_shell("vm-a")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.root / "calls").exists())

    def test_start_failure_is_reported(self):
        self.stub_services("idle", start_fails=True)
        self.assertNotEqual(self.run_shell("vm-a").returncode, 0)

    def shared_config(self):
        platform = self.fleet / "platform.bundle"
        platform.mkdir()
        for name in ("HardwareModel", "MachineIdentifier", "AuxiliaryStorage"):
            (platform / name).write_bytes(("platform-" + name).encode())
        base = self.fleet / "base.bundle"
        base.mkdir()
        (base / "Disk.img").write_bytes(b"\x51" * (8 * 65536))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port > 60999 or port < 20000:
            port = 40991
        cfg = {"baseName": "base", "platformName": "platform", "port": port}
        configs = self.fleet / "shared-disks"
        configs.mkdir()
        path = configs / "vm-a.json"
        path.write_text(json.dumps(cfg))
        self.env["VOS_NBD_MIN_HOST_FREE"] = "0"
        return path, cfg

    def test_shared_prepare_creates_diskless_bundle_and_agent(self):
        import plistlib
        path, cfg = self.shared_config()
        self.bundle.rename(self.fleet / "saved.bundle")
        result = self.run_shell("vm-a", "--prepare-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.bundle / "Disk.img").exists())
        self.assertEqual((self.bundle / "HardwareModel").read_bytes(), b"platform-HardwareModel")
        params = json.loads((self.bundle / "Parameters.txt").read_text())
        self.assertEqual(params["nbdURL"], "nbd://127.0.0.1:%d/virtualos-test" % cfg["port"])
        state = self.fleet / ".virtualos-nbd/vm-a"
        plist = plistlib.loads((state / "com.virtualos.disk.vm-a.plist").read_bytes())
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["EnvironmentVariables"]["VOS_NBD_PERSISTENT"], "1")
        self.assertFalse((state / "server.log").exists())
        self.assertEqual(self.run_shell("vm-a", "--prepare-only").returncode, 0)
        cfg["port"] += 1
        path.write_text(json.dumps(cfg))
        before = self.contents()
        self.assertNotEqual(self.run_shell("vm-a", "--prepare-only").returncode, 0)
        self.assertEqual(self.contents(), before)

    def test_runbook_forwards_prepare_only(self):
        self.shared_config()
        self.bundle.rename(self.fleet / "saved.bundle")
        result = subprocess.run(["bash", str(SCRIPT.with_name("runbook.sh")),
                                 "live", "vm-a", "--prepare-only"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.fleet / ".virtualos-nbd/vm-a/server.log").exists())
        self.assertFalse((self.bundle / "Disk.img").exists())

    def test_shared_config_refuses_local_disk_conversion(self):
        self.shared_config()
        self.configure({})
        (self.bundle / "Disk.img").write_bytes(b"guest-files")
        before = self.contents()
        self.assertNotEqual(self.run_shell("vm-a", "--prepare-only").returncode, 0)
        self.assertEqual(self.contents(), before)

    def test_shared_disk_starts_before_vm_and_is_reused(self):
        self.shared_disk_lifecycle()

    def test_compressed_shared_disk_starts_without_copying_seed_and_is_reused(self):
        self.shared_disk_lifecycle(compressed=True)

    def test_compressed_writes_shared_disk_starts_and_is_reused(self):
        self.shared_disk_lifecycle(compressed=True, compressed_writes=True)

    def test_compressed_write_config_refuses_missing_seed_and_unknown_format(self):
        path, cfg = self.shared_config()
        cfg['writeFormat'] = 'zlib-v1'
        path.write_text(json.dumps(cfg))
        result = self.run_shell('vm-a', '--prepare-only')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pinned compressed-v1 delta', result.stderr)
        cfg['writeFormat'] = 'unknown'
        path.write_text(json.dumps(cfg))
        self.assertNotEqual(self.run_shell('vm-a', '--prepare-only').returncode, 0)

    def shared_disk_lifecycle(self, compressed=False, compressed_writes=False):
        import time
        config_path, cfg = self.shared_config()
        delta = self.fleet / "seed.sqlite"
        with closing(sqlite3.connect(delta)) as db:
            db.execute("CREATE TABLE blocks (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
            db.execute("INSERT INTO blocks VALUES (3, ?)", (b"\xab" * 65536,))
            db.commit()
        cfg["delta"] = "seed.sqlite"
        if compressed:
            sys.path.insert(0,str(SCRIPT.parent.parent))
            from compressed_delta import build
            machine=self.fleet/'machine.raw'
            content=bytearray((self.fleet/'base.bundle/Disk.img').read_bytes())
            content[3*65536:4*65536]=b'\xab'*65536
            machine.write_bytes(content)
            compressed_path=self.root/'seed-compressed.sqlite'
            receipt=build(self.fleet/'base.bundle/Disk.img',machine,compressed_path,reserve=0)
            cfg.update(delta=str(compressed_path),deltaFormat='compressed-v1',deltaSHA256=receipt['delta_sha256'])
            if compressed_writes:
                cfg['writeFormat']='zlib-v1'
        config_path.write_text(json.dumps(cfg))
        self.bundle.rename(self.fleet / "saved.bundle")
        self.stub_services("idle")
        pid_path = self.root / "disk.pid"
        self.env["VOS_TEST_DISK_PID"] = str(pid_path)
        self.env["VOS_TEST_DISK_PLIST"] = str(self.root / "loaded-plist")
        launchctl = self.root / "bin/launchctl"
        launchctl.write_text('''#!/usr/bin/env python3
import json, os, pathlib, plistlib, subprocess, sys
args = sys.argv[1:]
if not any("com.virtualos.disk." in a for a in args):
    sys.exit(0)
pid_path = pathlib.Path(os.environ["VOS_TEST_DISK_PID"])
loaded = pathlib.Path(os.environ["VOS_TEST_DISK_PLIST"])
if args[0] == "print":
    if not pid_path.exists():
        sys.exit(1)
    print("pid = " + pid_path.read_text())
elif args[0] == "bootstrap":
    loaded.write_text(args[-1])
elif args[0] == "kickstart" and not pid_path.exists():
    cfg = plistlib.loads(pathlib.Path(loaded.read_text()).read_bytes())
    with open(cfg["StandardOutPath"], "ab") as out:
        proc = subprocess.Popen(cfg["ProgramArguments"],
            env=dict(os.environ, **cfg["EnvironmentVariables"]), stdout=out,
            stderr=out, start_new_session=True)
    pid_path.write_text(str(proc.pid))
''')
        # Check the actual NBD greeting at the instant the boot script requests
        # VM start, so a premature start fails this integration test.
        runbook = self.root / "tools/fleet/runbook.sh"
        runbook.write_text('''#!/bin/bash
python3 - <<'PY'
import os, socket, struct
with socket.create_connection(("127.0.0.1", int(os.environ["VOS_TEST_NBD_PORT"])), timeout=2) as sock:
    assert sock.recv(8) == struct.pack(">Q", 0x4E42444D41474943)
PY
[[ $? -eq 0 ]] || exit 9
printf "%s %s\\n" "$1" "$2" >> "$VOS_TEST_CALLS"
''')
        self.env["VOS_TEST_NBD_PORT"] = str(cfg["port"])
        def cleanup_server():
            if pid_path.exists():
                try:
                    os.killpg(int(pid_path.read_text()), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                time.sleep(0.05)
        self.addCleanup(cleanup_server)
        base = self.fleet / "base.bundle/Disk.img"
        before = base.read_bytes()
        result = self.run_shell("vm-a")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first_pid = pid_path.read_text()
        self.assertEqual((self.root / "calls").read_text(), "start vm-a\n")
        overlay = self.fleet / ".virtualos-nbd/vm-a/overlay.sqlite"
        with closing(sqlite3.connect(overlay)) as db:
            if compressed_writes:
                from compressed_write_overlay import decode
                db.create_function('vos_overlay_decode_v1',1,decode)
            row=db.execute("SELECT data FROM blocks WHERE idx=3").fetchone()
            if compressed:self.assertIsNone(row)
            else:self.assertEqual(row[0], b"\xab" * 65536)
        running_status = json.dumps({"instances": [{"name": "vm-a", "state": "running"}]})
        (self.root / "bin/curl").write_text("#!/usr/bin/env python3\nprint(" + repr(running_status) + ")\n")
        with socket.create_connection(("127.0.0.1", cfg["port"]), timeout=2) as guest:
            self.assertEqual(len(guest.recv(18)), 18)
            # Simulate an attached guest holding the one NBD session. Re-running
            # go-live must reuse the service without blocking on a new greeting.
            result = self.run_shell("vm-a")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(pid_path.read_text(), first_pid)
        self.assertEqual((self.root / "calls").read_text(), "start vm-a\n")
        self.assertEqual(base.read_bytes(), before)
        self.assertFalse((self.bundle / "Disk.img").exists())


if __name__ == "__main__":
    unittest.main()
