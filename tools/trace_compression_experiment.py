#!/usr/bin/env python3
"""Offline, synthetic-only VA/v1 trace compression benchmark.

This imports the reviewed codec registry but never reads guest trace files.
Every candidate must decode to the exact input records before it is measured.
CLI decompression timings include process startup; use them as end-to-end local
latency estimates rather than a claim about embedded library performance.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import struct
import subprocess
import time
from pathlib import Path

import ax_codec


SIZES = (1, 8, 64, 256)
SCENARIOS = ("steady", "mixed", "varied")
FORMATS = ("json", "jsonl", "va_frames", "va_batch")
COMPRESSORS = {
    "none": ((), ()),
    "gzip6": (("gzip", "-n", "-6", "-c"), ("gzip", "-d", "-c")),
    "zstd3": (("zstd", "-q", "-3", "-c"), ("zstd", "-q", "-d", "-c")),
    "brotli5": (("brotli", "-q", "5", "-c"), ("brotli", "-d", "-c")),
}


def observation(status: str, count: int = 0) -> dict:
    if status == "true":
        process, windows, error = 1, max(1, count), 0
    elif status == "false":
        process, windows, error = 1, 0, 0
    elif status == "error":
        process, windows, error = 0, 0, -25211
    else:
        process, windows, error = 0, 0, 0
    return dict(kind="observation", profile="finder_v1", profile_version=1,
                state="finder_window_present", status=status,
                process_count=process, window_count=windows, error_code=error)


def role(count: int) -> dict:
    return dict(kind="role_count", profile="terminal_v1", profile_version=1,
                role="AXMenuBarItem", count=count)


def episode(status: str, executed: bool) -> dict:
    return dict(kind="episode", profile="finder_v1", profile_version=1,
                intent="open_finder_window", action="dock_finder_click",
                verifier="finder_window_present", status=status,
                executed=executed, error_code=-25211 if status == "error" else 0)


def fixtures(scenario: str, size: int) -> list[dict]:
    if scenario == "steady":
        pool = [observation("true", 1)]
    elif scenario == "mixed":
        pool = [observation("false"), observation("true", 1), role(7),
                episode("true", True), observation("error")]
    elif scenario == "varied":
        pool = [observation("true", i % 5 + 1) if i % 4 == 0 else
                observation("false") if i % 4 == 1 else
                role((i * 37) % 256) if i % 4 == 2 else
                episode(("true", "false", "error", "unknown")[i % 4], bool(i % 2))
                for i in range(256)]
        # The four-way branch above would otherwise emit only the fourth
        # episode status. Replace its slots with a rotating outcome sequence.
        for i in range(3, len(pool), 4):
            pool[i] = episode(("true", "false", "error", "unknown")[(i // 4) % 4],
                              bool((i // 4) % 2))
    else:
        raise ValueError(scenario)
    return [pool[i % len(pool)].copy() for i in range(size)]


def frame(data: bytes) -> bytes:
    if len(data) > 65535:
        raise ValueError("frame exceeds 16-bit experimental bound")
    return struct.pack("!H", len(data)) + data


def unframe(data: bytes) -> list[bytes]:
    result, offset = [], 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("truncated length")
        length = struct.unpack_from("!H", data, offset)[0]
        offset += 2
        if length == 0 or offset + length > len(data):
            raise ValueError("truncated or empty frame")
        result.append(data[offset:offset + length])
        offset += length
    return result


def encode(records: list[dict], fmt: str) -> bytes:
    canonical = lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
    if fmt == "json":
        return canonical(records)
    if fmt == "jsonl":
        return b"\n".join(map(canonical, records)) + b"\n"
    if fmt == "va_frames":
        return b"".join(frame(ax_codec.encode(item)) for item in records)
    if fmt == "va_batch":
        return b"".join(frame(ax_codec.encode_batch(records[i:i + 64]))
                        for i in range(0, len(records), 64))
    raise ValueError(fmt)


def decode(data: bytes, fmt: str) -> list[dict]:
    if fmt == "json":
        return json.loads(data)
    if fmt == "jsonl":
        return [json.loads(line) for line in data.splitlines()]
    if fmt == "va_frames":
        return [ax_codec.decode(item) for item in unframe(data)]
    if fmt == "va_batch":
        return [record for batch in unframe(data)
                for record in ax_codec.decode_batch(batch)]
    raise ValueError(fmt)


def pipe(argv: tuple[str, ...], payload: bytes) -> bytes:
    result = subprocess.run(argv, input=payload, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, timeout=15)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} failed: {result.stderr.decode(errors='replace')}")
    if len(result.stdout) > 1_000_000:
        raise ValueError("codec output exceeds synthetic bound")
    return result.stdout


def run(repeats: int) -> dict:
    available = {name: name == "none" or shutil.which(commands[0][0]) is not None
                 for name, commands in COMPRESSORS.items()}
    rows = []
    for scenario in SCENARIOS:
        for size in SIZES:
            records = fixtures(scenario, size)
            for fmt in FORMATS:
                raw = encode(records, fmt)
                if decode(raw, fmt) != records:
                    raise AssertionError(f"raw roundtrip failed: {scenario}/{size}/{fmt}")
                for codec, (compress, decompress) in COMPRESSORS.items():
                    if not available[codec]:
                        continue
                    packed = pipe(compress, raw) if compress else raw
                    if (pipe(decompress, packed) if decompress else packed) != raw:
                        raise AssertionError(f"compression roundtrip failed: {codec}")
                    timings = []
                    for _ in range(repeats):
                        start = time.perf_counter_ns()
                        unpacked = pipe(decompress, packed) if decompress else packed
                        recovered = decode(unpacked, fmt)
                        timings.append((time.perf_counter_ns() - start) / 1_000_000)
                        if recovered != records:
                            raise AssertionError(f"record roundtrip failed: {scenario}/{size}/{fmt}/{codec}")
                    rows.append(dict(scenario=scenario, records=size, format=fmt,
                                     compressor=codec, raw_bytes=len(raw),
                                     payload_bytes=len(packed),
                                     decode_median_ms=round(statistics.median(timings), 4)))
    return dict(schema="synthetic-va-compression/v1", synthetic_only=True,
                transport_overhead_included=False,
                decode_includes_cli_startup=True, repeats=repeats,
                compressors_available=available, rows=rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--json", action="store_true", help="print complete machine-readable results")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error("--repeats must be 1-100")
    result = run(args.repeats)
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return
    print("Synthetic VA/v1 only; bytes exclude authenticated transport; ms include CLI startup")
    print("Missing compressors:", ", ".join(k for k, v in result["compressors_available"].items() if not v) or "none")
    print("scenario records format compressor bytes decode_ms")
    for row in result["rows"]:
        print(f"{row['scenario']:7} {row['records']:3} {row['format']:9} "
              f"{row['compressor']:7} {row['payload_bytes']:5} {row['decode_median_ms']:9.4f}")


if __name__ == "__main__":
    main()
