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


def main():
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
            overlay_dir = lab / "vos-nbd-bundle-bdtest"
            overlay_dir.mkdir(exist_ok=True)
            overlay = overlay_dir / "overlay.sqlite"

            env = dict(os.environ)
            env.update({
                "VOS_NBD_BUNDLE_ROOT": str(root),
                "VOS_NBD_BASE_NAME": "thebase",
                "VOS_NBD_BUNDLE_OVERLAY": str(overlay),
                "VOS_NBD_BUNDLE_PORT": str(port),
                "VOS_NBD_BASE_DELTA": str(delta),
                "VOS_NBD_MIN_HOST_FREE": str(256 * 1024**2),
            })
            proc = subprocess.Popen(
                ["bash", str(TOOLS / "computer-space.sh"), "--nbd-base-delta-serve"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            ready = None
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
                    ready = obj
                    break
            if ready is None:
                proc.kill()
                output = proc.stdout.read() if proc.stdout else ""
                print(json.dumps({"base_delta_serve_test": "no_listener", "output": output}))
                return 1

            # Connect as an NBD client.
            sock = socket.create_connection(("127.0.0.1", port), timeout=10)
            sock.settimeout(10)
            try:
                return _client_round_trip(sock, machine_blocks, proc, ready)
            except Exception as error:  # noqa: BLE001
                proc.terminate()
                try:
                    out = proc.stdout.read()
                except Exception:
                    out = ""
                print(json.dumps({"base_delta_serve_test": "FAILED",
                                  "error": repr(error), "server_output": out}))
                return 1
        finally:
            import shutil
            shutil.rmtree(lab, ignore_errors=True)


def _client_round_trip(sock, machine_blocks, proc, ready):
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

    changed_ok = payload == machine_blocks[3]
    base_ok = payload != bytes(BLOCK)  # base block 3 is zeros
    result = {
        "base_delta_serve_test": "passed" if (changed_ok and base_ok) else "FAILED",
        "delta_seeded": ready.get("delta_seeded"),
        "delta_total": ready.get("delta_total"),
        "block3_is_machine_block": changed_ok,
        "block3_not_base_block": base_ok,
    }
    sock.close()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["base_delta_serve_test"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
