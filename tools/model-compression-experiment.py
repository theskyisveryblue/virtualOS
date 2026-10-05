#!/usr/bin/env python3
"""Bounded, byte-exact compression benchmark for the local model JSON artifacts."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "models"
MAX_FILES = 32
MAX_FILE_BYTES = 2_000_000
MAX_INPUT_BYTES = 8_000_000
MAX_OUTPUT_BYTES = 16_000_000
TIMEOUT_SECONDS = 25


def digest(data):
    return hashlib.sha256(data).hexdigest()


def collect(scope):
    paths = sorted(ARTIFACTS.rglob("*.json"))
    if scope == "models":
        paths = [p for p in paths if p.name == "model.json" or (p.parent.name == "design" and not p.name.endswith(".metrics.json"))]
    if not paths or len(paths) > MAX_FILES:
        raise ValueError(f"expected 1–{MAX_FILES} JSON files, found {len(paths)}")
    files = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"not a regular file: {path}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"input file exceeds {MAX_FILE_BYTES} bytes: {path}")
        files.append((path.relative_to(ARTIFACTS).as_posix(), path.read_bytes()))
    if sum(len(body) for _, body in files) > MAX_INPUT_BYTES:
        raise ValueError("total input exceeds 8 MB")
    return files


def bundle(files):
    # Fixed framing, sorted paths, no timestamps or platform dependent tar fields.
    out = bytearray(b"VOSMODEL1")
    for name, body in files:
        encoded = name.encode("utf-8")
        out.extend(struct.pack(">IQ", len(encoded), len(body)))
        out.extend(encoded)
        out.extend(body)
    return bytes(out)


def unbundle(blob):
    if not blob.startswith(b"VOSMODEL1"):
        raise ValueError("invalid bundle header")
    pos = 9
    files = []
    while pos < len(blob):
        if len(blob) - pos < 12:
            raise ValueError("truncated bundle")
        name_len, body_len = struct.unpack_from(">IQ", blob, pos)
        pos += 12
        if name_len > 4096 or body_len > MAX_FILE_BYTES or pos + name_len + body_len > len(blob):
            raise ValueError("invalid bundle length")
        name = blob[pos:pos + name_len].decode("utf-8")
        pos += name_len
        body = blob[pos:pos + body_len]
        pos += body_len
        files.append((name, body))
    return files


CODECS = {
    "gzip": (["gzip", "-n", "-c"], ["gzip", "-d", "-c"], [1, 6, 9]),
    "zstd": (["zstd", "-q", "-c", "--no-dictID"], ["zstd", "-q", "-d", "-c"], [1, 3, 9, 19]),
    "brotli": (["brotli", "-c"], ["brotli", "-d", "-c"], [1, 5, 9, 11]),
    "xz": (["xz", "-c"], ["xz", "-d", "-c"], [0, 3, 6, 9]),
}


def args_for(codec, level):
    encoder, decoder, _ = CODECS[codec]
    if codec == "brotli":
        return encoder + ["-q", str(level)], decoder
    return encoder + [f"-{level}"], decoder


def run(command, data):
    start = time.perf_counter_ns()
    proc = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=TIMEOUT_SECONDS, check=False)
    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
    if proc.returncode:
        raise RuntimeError(f"{' '.join(command)} exited {proc.returncode}: {proc.stderr[:250]!r}")
    if len(proc.stdout) > MAX_OUTPUT_BYTES:
        raise ValueError("codec output exceeded 16 MB")
    return proc.stdout, elapsed_ms


def measure(codec, level, layout, files):
    enc, dec = args_for(codec, level)
    chunks = [bundle(files)] if layout == "bundle" else [body for _, body in files]
    originals = [digest(chunk) for chunk in chunks]
    compressed = []
    encode_ms = decode_ms = 0.0
    for chunk in chunks:
        packed, ms = run(enc, chunk)
        compressed.append(packed)
        encode_ms += ms
    for packed, original in zip(compressed, originals):
        decoded, ms = run(dec, packed)
        decode_ms += ms
        if digest(decoded) != original:
            raise ValueError("SHA-256 round trip mismatch")
        if layout == "bundle" and unbundle(decoded) != files:
            raise ValueError("bundle did not restore source file names and bytes")
    return {
        "scope": None, "layout": layout, "codec": codec, "level": level,
        "source_bytes": sum(map(len, chunks)),
        "compressed_bytes": sum(map(len, compressed)),
        "ratio": round(sum(map(len, compressed)) / sum(map(len, chunks)), 5),
        "encode_ms": round(encode_ms, 3), "decode_ms": round(decode_ms, 3),
        "sha256_roundtrip": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write detailed JSON results")
    args = parser.parse_args()
    rows = []
    manifest = {}
    for scope in ("models", "all-json"):
        files = collect(scope)
        manifest[scope] = {name: {"bytes": len(body), "sha256": digest(body)} for name, body in files}
        for layout in ("separate", "bundle"):
            for codec, (_, _, levels) in CODECS.items():
                if not shutil.which(codec):
                    print(f"Skipping unavailable codec: {codec}", file=sys.stderr)
                    continue
                for level in levels:
                    try:
                        row = measure(codec, level, layout, files)
                        row["scope"] = scope
                        rows.append(row)
                    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        print(f"Failed {scope}/{layout}/{codec}-{level}: {exc}", file=sys.stderr)
    result = {"format": 1, "manifest": manifest, "results": rows,
              "note": "Wall-clock timings include process startup and are single runs; peak RSS was not measured."}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    for scope in ("models", "all-json"):
        subset = [r for r in rows if r["scope"] == scope]
        print(f"\n{scope}: {len(manifest[scope])} files, {sum(v['bytes'] for v in manifest[scope].values())} payload bytes; {len(subset)} trials")
        for row in sorted(subset, key=lambda r: r["compressed_bytes"])[:8]:
            print(f"  {row['layout']:8} {row['codec']:6} {row['level']:2}  {row['compressed_bytes']:6} B  encode {row['encode_ms']:7.2f} ms  decode {row['decode_ms']:7.2f} ms")
    if not rows or len(rows) != 60:
        print(f"Completed {len(rows)}/60 expected trials", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
