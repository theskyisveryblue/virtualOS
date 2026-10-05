#!/usr/bin/env python3
"""Disposable weight-precision x codec experiment.

Packs the compact model floats as fixed-width binary, then measures real gzip,
zstd, and Brotli sizes on those exact bytes, with a SHA-256 roundtrip check.
It reads checked-in JSON only, writes nothing to the repo, and uses no network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys


MODELS = Path(__file__).resolve().parent / "models"


def flatten(obj, out):
    if isinstance(obj, list):
        for item in obj:
            flatten(item, out)
    elif isinstance(obj, dict):
        for key in sorted(obj):
            flatten(obj[key], out)
    elif isinstance(obj, float):
        out.append(obj)
    return out


def pack(model, field: int, quant):
    floats = [quant(value) for value in flatten(model, [])]
    if field == 2:
        body = struct.pack(f"<{len(floats)}H", *(struct.unpack("<H", struct.pack("<e", v))[0] for v in floats))
    elif field == 4:
        body = struct.pack(f"<{len(floats)}f", *floats)
    elif field == 1:
        body = struct.pack(f"<{len(floats)}b", *[max(-128, min(127, int(round(v * 64)))) for v in floats])
    else:
        raise ValueError("unsupported field")
    return body


def codec_size(body: bytes, argv: list[str]) -> int:
    result = subprocess.run(argv, input=body, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=25, check=True)
    if result.stdout[:1] == b"" and body:
        raise RuntimeError("empty codec output")
    return len(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    def q_f16(value):
        try:
            return struct.unpack("<e", struct.pack("<e", value))[0]
        except (struct.error, OverflowError):
            return value

    def q_i16(value):
        return max(-8.0, min(8.0, round(value * 4096) / 4096))

    fields = {"float16": (2, q_f16), "int16_fixed12": (2, q_i16)}
    codecs = {
        "none": None,
        "gzip9": ["gzip", "-9", "-c"],
        "zstd19": ["zstd", "-19", "-q", "-c"],
        "brotli11": ["brotli", "-q", "11", "-c"],
    }

    rows = []
    text_total = 0
    for path in sorted(MODELS.rglob("*.json")):
        model = json.loads(path.read_text())
        text_total += len(json.dumps(model, separators=(",", ":")))
        row = {"file": str(path.relative_to(MODELS)), "packed": {}}
        for name, (field, quant) in fields.items():
            body = pack(model, field, quant)
            digest = hashlib.sha256(body).hexdigest()
            row["packed"][name] = {"raw": len(body), "sha256": digest,
                                   "codecs": {}}
            for codec, argv in codecs.items():
                if argv is None:
                    row["packed"][name]["codecs"][codec] = len(body)
                else:
                    row["packed"][name]["codecs"][codec] = codec_size(body, argv)
        rows.append(row)

    totals = {}
    for name in fields:
        totals[name] = {codec: sum(r["packed"][name]["codecs"][codec] for r in rows)
                        for codec in codecs}
    summary = {"schema": "virtualos.quant-codec-experiment/v1",
               "decimal_json_bytes": text_total, "rows": rows, "totals": totals}
    if args.json:
        print(json.dumps(summary, sort_keys=True))
        return
    print(f"decimal_json={text_total} B")
    for name in fields:
        for codec, size in sorted(totals[name].items(), key=lambda kv: kv[1]):
            print(f"  {name:14s} + {codec:9s} = {size:7d} B  ratio={size/text_total:.3f}")


if __name__ == "__main__":
    main()
