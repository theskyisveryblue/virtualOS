#!/usr/bin/env python3
"""Disposable, deterministic API image-payload compression experiment."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
import random
import statistics
import struct
import subprocess
import tempfile
import time
import zlib


SIZE = 512


def png(kind: str, variant: int) -> bytes:
    rng = random.Random(2817 + variant)
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)
        for x in range(SIZE):
            if kind == "ui":
                header = y < 54
                sidebar = x < 118
                button = 165 + 16 * variant < x < 330 + 16 * variant and 110 < y < 155
                color = ((34, 41, 52) if header else (225, 232, 240) if sidebar
                         else (53, 123, 220) if button else (249, 250, 252))
            elif kind == "noise":
                color = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            else:
                raise ValueError("unknown fixture")
            rows.extend(color)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body)))

    head = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head)
            + chunk(b"IDAT", zlib.compress(rows, level=6)) + chunk(b"IEND", b""))


def median_ms(callback, repetitions=15) -> float:
    samples = []
    for _ in range(repetitions):
        start = time.perf_counter_ns()
        callback()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    return round(statistics.median(samples), 3)


def measures(left: bytes, right: bytes) -> dict:
    raw = struct.pack(">I", len(left)) + left
    body = json.dumps({"left_image_base64": base64.b64encode(left).decode(),
                       "right_image_base64": base64.b64encode(right).decode()},
                      separators=(",", ":")).encode()
    binary_pair = struct.pack(">II", len(left), len(right)) + left + right
    compressed = gzip.compress(body, compresslevel=6, mtime=0)
    if gzip.decompress(compressed) != body:
        raise AssertionError("gzip roundtrip failed")
    parsed = json.loads(body)
    if (base64.b64decode(parsed["left_image_base64"]) != left or
            base64.b64decode(parsed["right_image_base64"]) != right):
        raise AssertionError("JSON roundtrip failed")
    return {"left_image_bytes": len(left), "right_image_bytes": len(right),
            "single_binary_frame_bytes": len(raw),
            "pair_binary_frame_bytes": len(binary_pair), "pair_base64_json_bytes": len(body),
            "pair_gzip_json_bytes": len(compressed),
            "base64_over_binary_ratio": round(len(body) / len(binary_pair), 3),
            "gzip_over_binary_ratio": round(len(compressed) / len(binary_pair), 3),
            "json_build_median_ms": median_ms(lambda: json.dumps({
                "left_image_base64": base64.b64encode(left).decode(),
                "right_image_base64": base64.b64encode(right).decode()}, separators=(",", ":")).encode()),
            "gzip_median_ms": median_ms(lambda: gzip.compress(body, compresslevel=6, mtime=0)),
            "gunzip_median_ms": median_ms(lambda: gzip.decompress(compressed)),
            "left_sha256": hashlib.sha256(left).hexdigest(),
            "right_sha256": hashlib.sha256(right).hexdigest()}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="vos-api-payload-") as directory:
        root = Path(directory)
        result = {}
        for kind in ("ui", "noise"):
            images = []
            jpegs = []
            for variant in (0, 1):
                source = root / f"{kind}-{variant}.png"
                source.write_bytes(png(kind, variant))
                images.append(source.read_bytes())
                jpeg = root / f"{kind}-{variant}.jpg"
                subprocess.run(["/usr/bin/sips", "-s", "format", "jpeg", "-s",
                                "formatOptions", "80", str(source), "--out", str(jpeg)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15, check=True)
                jpegs.append(jpeg.read_bytes())
            result[f"{kind}_png"] = measures(*images)
            result[f"{kind}_jpeg80"] = measures(*jpegs)
    print(json.dumps({"schema": "virtualos.api-payload-experiment/v1",
                      "fixture_size": f"{SIZE}x{SIZE}", "pair_images_equal": False,
                      "results": result}, sort_keys=True))


if __name__ == "__main__":
    main()
