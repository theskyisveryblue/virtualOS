#!/usr/bin/env python3
"""Disposable sparse-disk archive experiment; never reads a VM disk."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import time


MIB = 1024 * 1024
LOGICAL = 32 * MIB
CHUNK = 1024 * 1024


def make_fixture(path: Path, kind: str) -> None:
    rng = random.Random(1097)
    with path.open("wb") as out:
        out.truncate(LOGICAL)
        if kind == "sparse_random":
            for offset in (0, 8 * MIB, 16 * MIB, 24 * MIB):
                out.seek(offset)
                for _ in range(2):
                    out.write(rng.randbytes(CHUNK))
        elif kind == "dense_zero":
            out.seek(0)
            for _ in range(LOGICAL // CHUNK):
                out.write(bytes(CHUNK))
        elif kind == "sparse_mixed":
            for offset in (0, 8 * MIB):
                out.seek(offset)
                for _ in range(2):
                    out.write(rng.randbytes(CHUNK))
            for offset in (16 * MIB, 24 * MIB):
                out.seek(offset)
                for _ in range(2):
                    out.write((b"virtualos-disposable-fixture\n" * 40000)[:CHUNK])
        else:
            raise ValueError("unsupported fixture")


def sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run(argv: list[str], destination: Path) -> float:
    start = time.perf_counter_ns()
    with destination.open("wb") as out:
        subprocess.run(argv, stdout=out, stderr=subprocess.PIPE,
                       timeout=40, check=True)
    return round((time.perf_counter_ns() - start) / 1_000_000, 2)


def measure(source: Path, root: Path, codec: str, level: int, expected: str) -> dict:
    archive = root / f"{codec}-{level}.bin"
    restored = root / "restored.raw"
    if codec == "gzip":
        encode = ["gzip", "-n", f"-{level}", "-c", str(source)]
        decode = ["gzip", "-d", "-c", str(archive)]
    else:
        encode = ["zstd", "-q", f"-{level}", "-c", str(source)]
        decode = ["zstd", "-q", "-d", "-c", str(archive)]
    encode_ms = run(encode, archive)
    decode_ms = run(decode, restored)
    if restored.stat().st_size != LOGICAL or sha(restored) != expected:
        raise AssertionError("archive did not restore exact fixture")
    result = {"codec": codec, "level": level, "archive_bytes": archive.stat().st_size,
              "encode_ms": encode_ms, "decode_ms": decode_ms,
              "sha256_roundtrip": True}
    archive.unlink()
    restored.unlink()
    return result


def main() -> None:
    if not shutil.which("zstd") or not shutil.which("gzip"):
        raise SystemExit("experiment requires gzip and zstd CLI")
    with tempfile.TemporaryDirectory(prefix="vos-disk-compression-") as directory:
        root = Path(directory)
        rows = []
        for kind in ("sparse_random", "dense_zero", "sparse_mixed"):
            fixture = root / "fixture.raw"
            make_fixture(fixture, kind)
            if fixture.stat().st_size != LOGICAL:
                raise AssertionError("fixture size changed")
            expected = sha(fixture)
            row = {"fixture": kind, "logical_bytes": LOGICAL,
                   "allocated_bytes": fixture.stat().st_blocks * 512,
                   "archives": [measure(fixture, root, codec, level, expected)
                                for codec, level in (("gzip", 1), ("gzip", 6),
                                                     ("zstd", 1), ("zstd", 3))]}
            rows.append(row)
            fixture.unlink()
    print(json.dumps({"schema": "virtualos.disposable-disk-compression/v1",
                      "synthetic_only": True, "rows": rows}, sort_keys=True))


if __name__ == "__main__":
    main()
