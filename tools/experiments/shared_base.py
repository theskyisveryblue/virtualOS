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
"""

import argparse
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


def main(argv):
    parser = argparse.ArgumentParser(description="shared-base fleet disk experiment")
    parser.add_argument("command", choices=["report", "diff"])
    parser.add_argument("args", nargs="+")
    parser.add_argument("--limit-blocks", type=int, default=None,
                        help="only scan the first N 64 KiB blocks (sampling)")
    ns = parser.parse_args(argv)
    if ns.command == "report":
        base, machines = ns.args[0], ns.args[1:]
        if not machines:
            raise SystemExit("report needs BASE and at least one MACHINE")
        print(json.dumps(report(base, machines, ns.limit_blocks), indent=2, sort_keys=True))
    elif ns.command == "diff":
        if len(ns.args) != 3:
            raise SystemExit("diff needs BASE MACHINE OUT_DIR")
        diff(ns.args[0], ns.args[1], ns.args[2], ns.limit_blocks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
