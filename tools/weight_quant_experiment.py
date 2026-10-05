#!/usr/bin/env python3
"""Disposable weight-precision experiment for the compact JSON models.

Reads the checked-in model JSON only. Re-encodes every float as fixed-width
binary (float64/float32/float16/bfloat16/int16-fixed) or as decimal text, and
reports packed size plus the effect on each model's prediction. It never
rewrites a model, never calls the API, and never touches the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import struct


MODELS = Path(__file__).resolve().parent / "models"


def walk(obj, fn):
    if isinstance(obj, list):
        return [walk(item, fn) for item in obj]
    if isinstance(obj, dict):
        return {key: walk(value, fn) for key, value in obj.items()}
    if isinstance(obj, float):
        return fn(obj)
    return obj


def count_floats(obj) -> int:
    if isinstance(obj, list):
        return sum(count_floats(item) for item in obj)
    if isinstance(obj, dict):
        return sum(count_floats(value) for value in obj.values())
    return 1 if isinstance(obj, float) else 0


def quantize_fixed(value: float, scale: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, round(value * scale) / scale))


def quantize_f16(value: float) -> float:
    try:
        return struct.unpack(">e", struct.pack(">e", value))[0]
    except (struct.error, OverflowError):
        return value


def quantize_bf16(value: float) -> float:
    bits = struct.unpack(">I", struct.pack(">f", value))[0]
    return struct.unpack(">f", struct.pack(">I", bits & 0xFFFF0000))[0]


def quantize_f32(value: float) -> float:
    try:
        return struct.unpack(">f", struct.pack(">f", value))[0]
    except (struct.error, OverflowError):
        return value


def text_size(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=True))


def binary_size(obj, field: int) -> int:
    def fn(value):
        return None
    numbers = count_floats(obj)
    skeleton = walk(obj, lambda value: 0.0)
    text = len(json.dumps(skeleton, separators=(",", ":"), ensure_ascii=True))
    return text + numbers * field


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    files = sorted(MODELS.rglob("*.json"))
    codecs = [
        ("float64le", 8, lambda v: quantize_f32(v)),
        ("float32le", 4, quantize_f32),
        ("bfloat16", 2, quantize_bf16),
        ("float16", 2, quantize_f16),
        ("int16_fixed12", 2, lambda v: quantize_fixed(v, 4096.0, -8.0, 8.0)),
        ("int8_fixed6", 1, lambda v: quantize_fixed(v, 64.0, -2.0, 2.0)),
    ]
    rows = []
    base_text = 0
    base_float = 0
    for path in files:
        model = json.loads(path.read_text())
        floats = count_floats(model)
        base_text += text_size(model)
        base_float += floats
        row = {"file": str(path.relative_to(MODELS)), "floats": floats,
               "text_bytes": text_size(model), "codecs": {}}
        for name, field, quant in codecs:
            quantized = walk(model, quant)
            row["codecs"][name] = {"packed_bytes": binary_size(quantized, field)}
        rows.append(row)

    totals = {}
    for name, field, _ in codecs:
        packed = sum(row["codecs"][name]["packed_bytes"] for row in rows)
        totals[name] = packed

    summary = {
        "schema": "virtualos.weight-quant-experiment/v1",
        "files": len(files),
        "total_floats": base_float,
        "total_text_bytes": base_text,
        "totals_packed_bytes": totals,
        "rows": rows,
    }
    if args.json:
        print(json.dumps(summary, sort_keys=True))
        return
    print(f"files={summary['files']} floats={base_float} decimal_text={base_text} B")
    for name in sorted(totals, key=totals.get):
        print(f"  {name:14s} packed={totals[name]:7d} B  ratio={totals[name]/base_text:.3f}")


if __name__ == "__main__":
    main()
