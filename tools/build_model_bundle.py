#!/usr/bin/env python3
"""Build the shipped model bundle from the checked-in JSON.

Writes the int16 binary bundle and, when Brotli is available, the compressed
form the API loads at start. Deterministic: identical JSON always produces
byte-identical output. Keeps a plain JSON fallback untouched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

import model_bundle

ROOT = Path(__file__).resolve().parent


def build(models_dir: Path, output: Path) -> dict:
    source = {str(path.relative_to(models_dir)): json.loads(path.read_text())
              for path in sorted(models_dir.rglob("*.json"))}
    raw = model_bundle.encode(source)
    text_bytes = sum(len(json.dumps(value, separators=(",", ":"))) for value in source.values())
    output.parent.mkdir(parents=True, exist_ok=True)
    codec = None
    if shutil.which("brotli") and output.suffix == ".br":
        compressed = subprocess.run(["brotli", "-q", "11", "-c"], input=raw,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=60, check=True).stdout
        output.write_bytes(compressed)
        codec = "brotli-11"
    else:
        output.write_bytes(raw)
    return {"output": str(output), "files": len(source), "json_bytes": text_bytes,
            "bundle_bytes": len(raw), "shipped_bytes": output.stat().st_size,
            "codec": codec}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-dir", type=Path, default=ROOT / "models")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.models_dir, args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
