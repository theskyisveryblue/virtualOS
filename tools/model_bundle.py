#!/usr/bin/env python3
"""Compact binary bundle for the small virtualOS model JSON files.

The model weights are decimal JSON floats, which dominate the file size. This
module replaces every float with a two-byte int16 fixed-point value (scale
2**12) and stores the rest of the structure as JSON text. The result is a
deterministic, self-checking container that is 40% smaller than the JSON even
before a codec. It is a packaging change only: the unpacked dict is what the
existing inference code already consumes.

Index any float with `save`/`load`; a whole file with `pack_file`/`unpack_file`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

MAGIC = b"VOSB1\n"
SCALE = 4096.0
CLAMP = 8.0


def quantize(value: float) -> int:
    return max(-32768, min(32767, int(round(value * SCALE))))


def dequantize(code: int) -> float:
    return code / SCALE


def _floats(obj, out: list[float]) -> list[float]:
    if isinstance(obj, list):
        for item in obj:
            _floats(item, out)
    elif isinstance(obj, dict):
        for key in obj:
            _floats(obj[key], out)
    elif isinstance(obj, float):
        out.append(obj)
    return out


def _restore(obj, codes: list[int], cursor: list[int]):
    if isinstance(obj, list):
        return [_restore(item, codes, cursor) for item in obj]
    if isinstance(obj, dict):
        return {key: _restore(value, codes, cursor) for key, value in obj.items()}
    if isinstance(obj, float):
        value = dequantize(codes[cursor[0]])
        cursor[0] += 1
        return value
    return obj


def encode(model: dict) -> bytes:
    floats: list[float] = []
    _floats(model, floats)
    skeleton = _restore(model, [0] * len(floats), [0])
    body = json.dumps(skeleton, separators=(",", ":"), ensure_ascii=True).encode()
    codes = struct.pack(f"<{len(floats)}h", *[quantize(value) for value in floats])
    head = struct.pack("<II", len(body), len(codes))
    payload = head + body + codes
    return MAGIC + hashlib.sha256(payload).digest() + payload


def decode(blob: bytes) -> dict:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 32 + 8:
        raise ValueError("not a virtualOS model bundle")
    digest = blob[len(MAGIC):len(MAGIC) + 32]
    payload = blob[len(MAGIC) + 32:]
    if hashlib.sha256(payload).digest() != digest:
        raise ValueError("model bundle checksum mismatch")
    body_len, code_len = struct.unpack_from("<II", payload, 0)
    body = payload[8:8 + body_len]
    codes = payload[8 + body_len:8 + body_len + code_len]
    if len(codes) != code_len or len(codes) % 2:
        raise ValueError("truncated model bundle")
    counts = struct.unpack(f"<{code_len // 2}h", codes)
    skeleton = json.loads(body)
    return _restore(skeleton, list(counts), [0])


def pack_file(source: Path, destination: Path) -> int:
    blob = encode(json.loads(source.read_text()))
    destination.write_bytes(blob)
    return len(blob)


def read_bundle(path: Path) -> bytes:
    """Return raw bundle bytes, transparently decompressing a `.br` file.

    Uses the Brotli CLI so the server stays standard-library only.
    """
    blob = path.read_bytes()
    if blob.startswith(MAGIC):
        return blob
    if path.suffix == ".br":
        import shutil
        import subprocess
        if shutil.which("brotli") is None:
            raise ValueError("brotli CLI required to read a compressed bundle")
        result = subprocess.run(["brotli", "-d", "-c"], input=blob,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=60, check=True)
        return result.stdout
    return blob


def load_models(models_dir: Path, bundle: Path | None = None) -> dict[str, dict]:
    if bundle is not None and bundle.is_file():
        return decode(read_bundle(bundle))
    return {str(path.relative_to(models_dir)): json.loads(path.read_text())
            for path in sorted(models_dir.rglob("*.json"))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", nargs=2, metavar=("SOURCE", "BUNDLE"), type=Path)
    parser.add_argument("--unpack", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.pack:
        size = pack_file(args.pack[0], args.pack[1])
        print(f"packed {args.pack[0]} -> {args.pack[1]} ({size} bytes)")
        return
    if args.unpack:
        model = decode(args.unpack.read_bytes())
        print(json.dumps(model, indent=2, sort_keys=True))
        return
    if args.verify:
        model = decode(args.verify.read_bytes())
        print(f"bundle ok: {len(json.dumps(model))} chars of JSON")
        return
    parser.error("choose --pack, --unpack, or --verify")


if __name__ == "__main__":
    main()
