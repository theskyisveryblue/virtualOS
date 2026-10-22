#!/usr/bin/env python3
"""Integration test: serve a machine as shared-base + delta over NBD, from a client.

This is the P1 end-to-end scale step at the socket level: start a `base-delta-serve`
listener on loopback, connect as an NBD client, negotiate with NBD_OPT_GO, read a
block the machine changed, and assert it equals the machine's block (not the
base's). Fully isolated and disposable: images and overlay live under a temp dir;
nothing here is production and nothing is written to the base.

Usage: python3 tools/experiments/base_delta_serve_test.py
"""

import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS))

from cloud_disk import (  # noqa: E402
    NBD_MAGIC, NBD_OPTION_MAGIC, NBD_OPTION_REPLY_MAGIC, NBD_REQUEST_MAGIC,
    NBD_SIMPLE_REPLY_MAGIC, NBD_TRANSMISSION_FLAGS, BLOCK,
    base_delta_overlay,
)


def recv_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        piece = sock.recv(count - len(data))
        if not piece:
            raise EOFError("peer closed")
        data.extend(piece)
    return bytes(data)


def build_images(tmp, blocks=8):
    base = tmp / "base.img"
    machine = tmp / "machine.img"
    with base.open("wb") as stream:
        for i in range(blocks):
            stream.write(bytes([(i * 5) % 256]) * BLOCK if i % 2 == 0 else bytes(BLOCK))
    machine_blocks = []
    for i in range(blocks):
        block = (b"\xab" * BLOCK if i == 3 else
                 b"\xcd" * BLOCK if i == 5 else
                 (bytes([(i * 5) % 256]) * BLOCK if i % 2 == 0 else bytes(BLOCK)))
        machine_blocks.append(block)
    with machine.open("wb") as stream:
        for block in machine_blocks:
            stream.write(block)
    delta = tmp / "delta.sqlite"
    db = sqlite3.connect(str(delta))
    db.execute("CREATE TABLE blocks (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
    for i, block in enumerate(machine_blocks):
        base_block = (bytes([(i * 5) % 256]) * BLOCK if i % 2 == 0 else bytes(BLOCK))
        if block != base_block:
            db.execute("INSERT INTO blocks VALUES (?,?)", (i, block))
    db.commit()
    db.close()
    return base, machine_blocks, delta


def main(persistent=False):
    with tempfile.TemporaryDirectory(prefix="vos-bdserve-") as temp:
        tmp = Path(temp)
        # The CLI enforces /private/tmp and a bundle layout; use the real tmp root.
        lab = Path("/private/tmp") / ("vos-nbd-bundle-bdtest-%d" % os.getpid())
        lab.mkdir(parents=True, exist_ok=True)
        try:
            root = lab / "root"
            root.mkdir()
            base_bundle = root / "thebase.bundle"
            base_bundle.mkdir()
            base, machine_blocks, delta = build_images(lab, blocks=8)
            os.replace(base, base_bundle / "Disk.img")

            port = 40990
            overlay_dir = (root / ".virtualos-nbd" / "bdtest" if persistent else
                           lab / "vos-nbd-bundle-bdtest")
            overlay_dir.mkdir(parents=True, exist_ok=True)
            overlay = overlay_dir / "overlay.sqlite"
            if persistent:
                # Reject misplaced state and symlink escape before serving.
                for bad in (lab / "outside.sqlite", root / ".virtualos-nbd/x/bad.sqlite"):
                    try:
                        base_delta_overlay(root, bad, True)
                        raise AssertionError("invalid persistent path accepted")
                    except ValueError:
                        pass
                escape = root / ".virtualos-nbd/escape"
                escape.symlink_to(lab, target_is_directory=True)
                try:
                    base_delta_overlay(root, escape / "overlay.sqlite", True)
                    raise AssertionError("symlink escape accepted")
                except ValueError:
                    pass

            env = dict(os.environ)
            env.update({
                "VOS_NBD_BUNDLE_ROOT": str(root),
                "VOS_NBD_BASE_NAME": "thebase",
                "VOS_NBD_BUNDLE_OVERLAY": str(overlay),
                "VOS_NBD_BUNDLE_PORT": str(port),
                "VOS_NBD_BASE_DELTA": str(delta),
                "VOS_NBD_MIN_HOST_FREE": str(256 * 1024**2),
                "VOS_NBD_PERSISTENT": "1" if persistent else "0",
            })
            base_bytes = (base_bundle / "Disk.img").read_bytes()
            proc = None
            try:
                for restarted in (False, True):
                    proc, ready = _start_server(env)
                    assert ready["persistent"] == persistent
                    assert ready["overlay"] == str(overlay)
                    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                        sock.settimeout(10)
                        _client_round_trip(sock, machine_blocks, restarted)
                    assert ready["delta_seeded"] == (0 if restarted else 2)
                    if persistent and not restarted:
                        # Cross the lab's eight-session cap without losing the
                        # listener or resetting any acknowledged writes.
                        for _ in range(8):
                            with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                                sock.settimeout(10)
                                _client_round_trip(sock, machine_blocks, True)
                        contender_env = dict(env, VOS_NBD_BUNDLE_PORT=str(port + 1))
                        contender = subprocess.run(
                            ["bash", str(TOOLS / "computer-space.sh"), "--nbd-base-delta-serve"],
                            env=contender_env, capture_output=True, text=True, timeout=10)
                        assert contender.returncode != 0, "two servers acquired the same overlay"
                        assert "BlockingIOError" in contender.stderr, contender.stderr
                    _stop_server(proc)
                    proc = None
                assert (base_bundle / "Disk.img").read_bytes() == base_bytes
                print(json.dumps({"base_delta_serve_test": "passed", "delta_total": 2,
                                  "persistent": persistent,
                                  "nine_connections_verified": persistent,
                                  "exclusive_overlay_lock_verified": persistent,
                                  "persistent_path_guards_verified": persistent,
                                  "block3_is_machine_block": True,
                                  "guest_write_survived_server_restart": True,
                                  "guest_zero_survived_server_restart": True,
                                  "restart_blocks_reseeded": 0, "writes_to_base": 0}, sort_keys=True))
                return 0
            except Exception as error:  # noqa: BLE001
                out = _stop_server(proc) if proc is not None else ""
                print(json.dumps({"base_delta_serve_test": "FAILED",
                                  "error": repr(error), "server_output": out}))
                return 1
        finally:
            import shutil
            shutil.rmtree(lab, ignore_errors=True)


def _stop_server(proc):
    # The shell launches a Python child: terminate only this test's isolated
    # process group so a restart cannot connect to an orphaned old listener.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        output, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate(timeout=5)
    return output


def _start_server(env):
    proc = subprocess.Popen(
        ["bash", str(TOOLS / "computer-space.sh"), "--nbd-base-delta-serve"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("base_delta_listener") == "ready":
            return proc, obj
    output = _stop_server(proc)
    raise RuntimeError("no base-delta listener: " + output)


def _client_round_trip(sock, machine_blocks, restarted):
    magic, option_magic, _ = struct.unpack(">QQH", recv_exact(sock, 18))
    assert magic == NBD_MAGIC and option_magic == NBD_OPTION_MAGIC, "bad greeting"
    sock.sendall(struct.pack(">I", 0))  # client flags: no NBD_FLAG_C_FIXED_NEWSTYLE
    export = b"virtualos-test"
    info = struct.pack(">I", len(export)) + export + struct.pack(">H", 0)
    sock.sendall(struct.pack(">QII", NBD_OPTION_MAGIC, 7, len(info)) + info)
    # Read replies until NBD_OPT_GO is acked (or an error).
    negotiated = False
    while True:
        rmagic, ropt, rtype, rlen = struct.unpack(">QIII", recv_exact(sock, 20))
        recv_exact(sock, rlen) if rlen else b""
        if rmagic != NBD_OPTION_REPLY_MAGIC or ropt != 7:
            raise AssertionError("bad option reply")
        if rtype & 0x80000000:
            raise AssertionError("server refused export: type=%#x" % rtype)
        if rtype == 1:  # NBD_REP_ACK
            negotiated = True
            break
    assert negotiated, "no ack"

    # Read block 3, which the machine changed and the base did not.
    cookie = 0x1122334455667788
    sock.sendall(struct.pack(">IHHQQI", NBD_REQUEST_MAGIC, 0, 0, cookie,
                             3 * BLOCK, BLOCK))
    reply_magic, error, rcookie = struct.unpack(">IIQ", recv_exact(sock, 16))
    payload = recv_exact(sock, BLOCK) if error == 0 else b""
    assert reply_magic == NBD_SIMPLE_REPLY_MAGIC and rcookie == cookie, "bad reply"
    assert error == 0, "read error %d" % error

    expected = b"\x77" * BLOCK if restarted else machine_blocks[3]
    assert payload == expected, "guest block was reset to its original delta after restart"
    assert payload != bytes(BLOCK)
    if restarted:
        sock.sendall(struct.pack(">IHHQQI", NBD_REQUEST_MAGIC, 0, 0, cookie + 1, 5 * BLOCK, BLOCK))
        reply_magic, error, rcookie = struct.unpack(">IIQ", recv_exact(sock, 16))
        assert reply_magic == NBD_SIMPLE_REPLY_MAGIC and error == 0 and rcookie == cookie + 1
        assert recv_exact(sock, BLOCK) == bytes(BLOCK), "guest zero was reset after restart"
    else:
        # FUA acknowledges only after the write reaches the durable overlay.
        for number, data in ((3, b"\x77" * BLOCK), (5, bytes(BLOCK))):
            sock.sendall(struct.pack(">IHHQQI", NBD_REQUEST_MAGIC, 1, 1,
                                     cookie + number, number * BLOCK, BLOCK) + data)
            reply_magic, error, rcookie = struct.unpack(">IIQ", recv_exact(sock, 16))
            assert reply_magic == NBD_SIMPLE_REPLY_MAGIC and error == 0 and rcookie == cookie + number


if __name__ == "__main__":
    result = main()
    if result == 0:
        result = main(persistent=True)
    raise SystemExit(result)
