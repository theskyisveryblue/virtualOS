#!/usr/bin/env python3
"""Shared-base fleet disk experiment (ISOLATED).

Thesis: a fleet of macOS machines should not store N full disks in the cloud.
They share almost every block, so the fleet stores ONE immutable base plus a
tiny per-machine delta. This module measures the real sharing and can materialise
a machine's delta as a block overlay.

Separation rules (see README.md):
  * The stable disk plane (tools/cloud_disk*.py, tools/computer-space.sh) is not
    modified or imported for state. This experiment is standalone stdlib Python.
  * It only READS disk images. It never writes to, or references, the production
    machine store. Callers pass explicit paths under an experiment root.
  * It keeps its own objects/overlays under /private/tmp or an explicit dir.

Commands:
  report BASE MACHINE...        block-level sharing + fleet storage estimate
  diff   BASE MACHINE OUT_DIR   write MACHINE's delta vs BASE as a block overlay
  verify BASE MACHINE OUT_DIR   prove BASE+delta reproduces MACHINE byte-for-byte
"""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

BLOCK = 64 * 1024
PRODUCTION_ROOT = (Path.home() / "VirtualOSMachines").resolve()


def _guard(path):
    resolved = Path(path).resolve()
    if str(resolved) == str(PRODUCTION_ROOT) or str(resolved).startswith(str(PRODUCTION_ROOT) + os.sep):
        raise SystemExit("experiment refused: production machine store is off limits")
    if not resolved.is_file():
        raise SystemExit("experiment refused: not a regular file: %s" % resolved)
    return resolved


def block_hashes(path, limit_blocks=None):
    """Map block index -> sha256 for every non-zero block (read-only)."""
    path = _guard(path)
    result = {}
    with open(path, "rb", buffering=0) as stream:
        index = 0
        while True:
            if limit_blocks is not None and index >= limit_blocks:
                break
            raw = stream.read(BLOCK)
            if not raw:
                break
            if raw.count(0) != len(raw):
                result[index] = hashlib.sha256(raw).hexdigest()
            index += 1
    return result


def block_size(path):
    return _guard(path).stat().st_size


def _chunks_from_blocks(blocks, chunk_blocks=(4 * 1024 * 1024) // BLOCK):
    """Collapse 64 KiB block hashes into 4 MiB chunk hashes for storage math."""
    chunks = {}
    current = []
    start = None
    for index in sorted(blocks):
        if start is None:
            start = index
        expected = start + len(current)
        if index != expected or len(current) >= chunk_blocks:
            chunks[start] = hashlib.sha256(",".join(current).encode()).hexdigest()
            start = index
            current = []
        current.append(blocks[index])
    if current:
        chunks[start] = hashlib.sha256(",".join(current).encode()).hexdigest()
    return chunks


def _label(path, base=None):
    """Human label for a disk: its bundle directory name, or the file name."""
    p = Path(path)
    if p.name == "Disk.img":
        return p.parent.name
    return p.name


def report(base, machines, limit_blocks=None):
    base_map = block_hashes(base, limit_blocks)
    base_chunks = set(_chunks_from_blocks(base_map).values())
    chunk_mib = (4 * 1024 * 1024) / (1024 * 1024)
    per_machine = {}
    union_chunks = set(base_chunks)
    total_chunks_naive = len(base_chunks)
    for machine in machines:
        machine_map = block_hashes(machine, limit_blocks)
        machine_chunks = set(_chunks_from_blocks(machine_map).values())
        delta = machine_chunks - base_chunks
        shared = machine_chunks & base_chunks
        per_machine[_label(machine)] = {
            "nonzero_blocks": len(machine_map),
            "chunks": len(machine_chunks),
            "shared_with_base": len(shared),
            "delta_chunks": len(delta),
            "delta_pct_of_base": round(100.0 * len(delta) / max(1, len(base_chunks)), 3),
            "incremental_mib_after_base": round(len(delta) * chunk_mib, 1),
            "full_mib_if_stored_alone": round(len(machine_chunks) * chunk_mib, 1),
        }
        union_chunks |= machine_chunks
        total_chunks_naive += len(machine_chunks)
    return {
        "block_size": BLOCK,
        "base": _label(base),
        "base_chunks": len(base_chunks),
        "base_mib": round(len(base_chunks) * chunk_mib, 1),
        "machines": per_machine,
        "union_chunks_one_copy": len(union_chunks),
        "union_mib_one_copy": round(len(union_chunks) * chunk_mib, 1),
        "naive_chunks_all_disks": total_chunks_naive,
        "fleet_saving_pct": round(
            100.0 * (1 - len(union_chunks) / max(1, total_chunks_naive)), 3),
        "note": "chunk = 4 MiB of raw content; sizes are upper bounds before compression",
    }


def diff(base, machine, out_dir, limit_blocks=None):
    """Write MACHINE's differing blocks vs BASE into a fresh overlay directory."""
    base_map = block_hashes(base, limit_blocks)
    machine_path = _guard(machine)
    out = Path(out_dir)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    overlay = out / "delta.sqlite"
    if overlay.exists():
        overlay.unlink()
    db = sqlite3.connect(str(overlay))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE blocks (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
    db.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    changed = 0
    written = 0
    with open(machine_path, "rb", buffering=0) as stream:
        index = 0
        while True:
            if limit_blocks is not None and index >= limit_blocks:
                break
            raw = stream.read(BLOCK)
            if not raw:
                break
            digest = None if raw.count(0) == len(raw) else hashlib.sha256(raw).hexdigest()
            if base_map.get(index) != digest:
                db.execute("INSERT OR REPLACE INTO blocks VALUES (?,?)", (index, raw))
                changed += 1
                written += len(raw)
            index += 1
    db.commit()
    db.close()
    manifest = {
        "base": _label(base),
        "machine": _label(machine_path),
        "changed_blocks": changed,
        "delta_bytes": written,
        "total_blocks": index,
        "delta_pct": round(100.0 * changed / max(1, index), 4),
    }
    (out / "delta.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(json.dumps({"shared_base_diff": manifest}, sort_keys=True))
    return manifest


def verify(base, machine, delta_dir, limit_blocks=None):
    """Prove that BASE + the delta overlay reconstructs MACHINE byte-for-byte.

    Reads the base and machine read-only, reads delta.sqlite, and streams a
    block-by-block comparison. Does not materialise the reconstructed disk, so it
    needs no extra space next to a 30 GiB image.
    """
    base_path = _guard(base)
    machine_path = _guard(machine)
    delta_sqlite = Path(delta_dir) / "delta.sqlite"
    if not delta_sqlite.is_file():
        raise SystemExit("verify refused: no delta.sqlite in %s" % delta_dir)
    checked = mismatches = 0
    machine_total = delta_bytes = 0
    with closing(sqlite3.connect(delta_sqlite.resolve().as_uri() + "?mode=ro", uri=True)) as db, \
            open(base_path, "rb", buffering=0) as bstream, \
            open(machine_path, "rb", buffering=0) as mstream:
        # A real machine delta can be gigabytes. Stream one sorted block at a
        # time while a read transaction pins a consistent SQLite snapshot.
        db.execute("BEGIN")
        delta_count = db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        rows = iter(db.execute("SELECT idx, data FROM blocks WHERE idx>=0 ORDER BY idx"))
        current = next(rows, None)
        index = 0
        while True:
            if limit_blocks is not None and index >= limit_blocks:
                break
            raw = mstream.read(BLOCK)
            if not raw:
                break
            # The reconstructed block is the delta's, if present, else the base's.
            if current is not None and current[0] == index:
                reconstructed = bytes(current[1])
                delta_bytes += len(reconstructed)
                current = next(rows, None)
            else:
                bstream.seek(index * BLOCK)
                reconstructed = bstream.read(len(raw))
                # The base may be shorter than the machine; the disk is
                # zero-extended, so a missing tail block is zeros, not empty.
                if len(reconstructed) < len(raw):
                    reconstructed = reconstructed + b"\0" * (len(raw) - len(reconstructed))
            checked += 1
            if hashlib.sha256(reconstructed).hexdigest() != hashlib.sha256(raw).hexdigest():
                mismatches += 1
            machine_total += len(raw)
            index += 1

        # Negative indexes and blocks past the compared machine are invalid.
        stray = db.execute("SELECT COUNT(*) FROM blocks WHERE idx<0 OR idx>=?", (checked,)).fetchone()[0]
    manifest = {
        "base": _label(base),
        "machine": _label(machine),
        "blocks_checked": checked,
        "mismatches": mismatches,
        "stray_delta_blocks": stray,
        "delta_blocks": delta_count,
        "delta_bytes": delta_bytes,
        "logical_bytes": machine_total,
        "round_trip_ok": mismatches == 0 and stray == 0,
        "note": "base + delta reconstructed and compared against the machine block-by-block",
    }
    print(json.dumps({"shared_base_verify": manifest}, sort_keys=True))
    receipt = os.environ.get("VOS_EXP_RECEIPT")
    if receipt:
        Path(receipt).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    if not manifest["round_trip_ok"]:
        raise SystemExit("verify FAILED: %d mismatches, %d stray blocks" % (mismatches, stray))
    return manifest


def selftest():
    """Synthesise a base and a machine, diff + verify, and assert the round trip.

    Fast and fully isolated: writes tiny images under /private/tmp, never touches
    a real disk. This is the regression guard for the shared-base method.
    """
    import tempfile

    blk = 64 * 1024
    with tempfile.TemporaryDirectory(prefix="vos-shared-base-selftest-") as tmp:
        tmp = Path(tmp)
        base = tmp / "base.img"
        machine = tmp / "machine.img"
        with open(base, "wb") as f:
            for i in range(64):
                f.write(bytes([(i * 7) % 256]) * blk if i % 3 == 0 else b"\0" * blk)
        with open(machine, "wb") as f:
            for i in range(64):
                if i == 5:
                    f.write(b"\xaa" * blk)
                elif i == 17:
                    f.write(os.urandom(blk))
                else:
                    f.write(bytes([(i * 7) % 256]) * blk if i % 3 == 0 else b"\0" * blk)
            f.write(b"\xbb" * blk)
            f.write(b"\xcc" * blk)

        delta_dir = tmp / "delta"
        diff(base, machine, delta_dir)
        good = verify(base, machine, delta_dir)

        # Corrupt one delta block and prove verify rejects it.
        db = sqlite3.connect(str(delta_dir / "delta.sqlite"))
        db.execute("UPDATE blocks SET data=? WHERE idx=5", (b"\x00" * blk,))
        db.commit()
        db.close()
        caught = False
        try:
            verify(base, machine, delta_dir)
        except SystemExit:
            caught = True

        # A negative index must not silently disappear from reconstruction.
        with sqlite3.connect(str(delta_dir / "delta.sqlite")) as db:
            db.execute("UPDATE blocks SET data=? WHERE idx=5", (b"\xaa" * blk,))
            db.execute("INSERT INTO blocks VALUES (?,?)", (-1, bytes(blk)))
        negative_caught = False
        try:
            verify(base, machine, delta_dir)
        except SystemExit:
            negative_caught = True

        # Bound Python memory independently of the total delta size: this
        # fixture previously required retaining all 4 MiB of changed blocks.
        import tracemalloc
        with machine.open("wb") as stream:
            for _ in range(66):
                stream.write(b"\x5a" * blk)
        diff(base, machine, delta_dir)
        tracemalloc.start()
        try:
            streamed = verify(base, machine, delta_dir)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert streamed["round_trip_ok"] and peak < 1024**2, "delta verifier retained the whole delta"

    result = {
        "shared_base_selftest": "passed" if (good["round_trip_ok"] and caught and negative_caught) else "FAILED",
        "round_trip_ok": good["round_trip_ok"],
        "corruption_caught": caught,
        "blocks_checked": good["blocks_checked"],
        "negative_index_caught": negative_caught,
        "streamed_verifier_peak_python_bytes": peak,
    }
    print(json.dumps(result, sort_keys=True))
    if result["shared_base_selftest"] != "passed":
        raise SystemExit("shared_base selftest FAILED")
    return result


def main(argv):
    parser = argparse.ArgumentParser(description="shared-base fleet disk experiment")
    parser.add_argument("command", choices=["report", "diff", "verify", "selftest"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--limit-blocks", type=int, default=None,
                        help="only scan the first N 64 KiB blocks (sampling)")
    ns = parser.parse_args(argv)
    if ns.command == "selftest":
        selftest()
        return 0
    if ns.command == "report":
        base, machines = ns.args[0], ns.args[1:]
        if not machines:
            raise SystemExit("report needs BASE and at least one MACHINE")
        print(json.dumps(report(base, machines, ns.limit_blocks), indent=2, sort_keys=True))
    elif ns.command == "diff":
        if len(ns.args) != 3:
            raise SystemExit("diff needs BASE MACHINE OUT_DIR")
        diff(ns.args[0], ns.args[1], ns.args[2], ns.limit_blocks)
    elif ns.command == "verify":
        if len(ns.args) != 3:
            raise SystemExit("verify needs BASE MACHINE OUT_DIR")
        verify(ns.args[0], ns.args[1], ns.args[2], ns.limit_blocks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
