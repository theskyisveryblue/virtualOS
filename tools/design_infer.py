#!/usr/bin/env python3
"""Dependency-free macOS inference for the small design-principle rankers."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import struct
import subprocess
import tempfile

KINDS = ("alignment", "overlap", "whitespace")


def channel_stats(plane: list[list[float]]) -> list[float]:
    values = [value for row in plane for value in row]
    mean = sum(values) / len(values)
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    tiles = []
    for top in range(0, len(plane), 8):
        for left in range(0, len(plane[0]), 8):
            samples = [value for row in plane[top:top + 8] for value in row[left:left + 8]]
            tiles.append(sum(samples) / len(samples))
    return [mean, std, *tiles]


def spread(values: list[float]) -> float:
    average = sum(values) / len(values)
    return math.sqrt(sum((item - average) ** 2 for item in values) / len(values))


def edge_stats(edge: list[list[float]]) -> list[float]:
    flat = [value for row in edge for value in row]
    height, width = len(edge), len(edge[0])
    column_means = [sum(edge[row][column] for row in range(height)) / height
                    for column in range(width)]
    row_means = [sum(row) / width for row in edge]
    return [sum(flat) / len(flat), spread(flat), spread(column_means),
            spread(row_means), *channel_stats(edge)[2:]]


def features(path: Path) -> list[float]:
    if not path.is_file() or path.stat().st_size > 10_000_000:
        raise ValueError("bounded image file required")
    with tempfile.TemporaryDirectory(prefix="vos-design-") as directory:
        bmp = Path(directory) / "small.bmp"
        subprocess.run(["/usr/bin/sips", "-z", "64", "64", "-s", "format", "bmp",
                        str(path), "--out", str(bmp)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=True)
        data = bmp.read_bytes()
    if (len(data) < 138 or data[:2] != b"BM" or len(data) > 100_000):
        raise ValueError("unsupported converted image")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height, planes, bits = struct.unpack_from("<iiHH", data, 18)
    if width != 64 or abs(height) != 64 or planes != 1 or bits not in (24, 32):
        raise ValueError("unexpected BMP geometry")
    pixel_bytes = bits // 8
    row_stride = ((width * bits + 31) // 32) * 4
    pixels = []
    for row in range(64):
        actual_row = row if height < 0 else 63 - row
        start = offset + actual_row * row_stride
        if start + row_stride > len(data):
            raise ValueError("truncated BMP")
        pixels.append([(data[start + column * pixel_bytes + 2] / 255,
                        data[start + column * pixel_bytes + 1] / 255,
                        data[start + column * pixel_bytes] / 255)
                       for column in range(64)])
    planes_rgb = [[[pixels[row][column][index] for column in range(64)]
                   for row in range(64)] for index in range(3)]
    gray = [[0.299 * r + 0.587 * g + 0.114 * b for r, g, b in row]
            for row in pixels]
    saturation = [[max(pixel) - min(pixel) for pixel in row] for row in pixels]
    output = []
    for plane in (*planes_rgb, gray, saturation):
        output.extend(channel_stats(plane))
    edge_x = [[float(abs(row[column + 1] - row[column]) > 0.12)
               for column in range(63)] for row in gray]
    edge_y = [[float(abs(gray[row + 1][column] - gray[row][column]) > 0.12)
               for column in range(64)] for row in range(63)]
    output.extend(edge_stats(edge_x))
    output.extend(edge_stats(edge_y))
    return output


def load_models(models: Path | dict) -> dict:
    if isinstance(models, dict):
        return models
    return {kind: json.loads((models / f"{kind}.json").read_text()) for kind in KINDS}


def compare(left: Path, right: Path, models: Path | dict, personal: Path | None = None) -> dict:
    first, second = features(left), features(right)
    loaded = load_models(models)
    result = {}
    for kind in KINDS:
        model = loaded[kind]
        if (model.get("schema") != "virtualos.design-ranker/v1"
                or model.get("kind") != kind or len(model["weights"]) != len(first)
                or len(model["scale"]) != len(first)):
            raise ValueError("invalid design model")
        delta = sum((a - b) * weight / scale for a, b, weight, scale in
                    zip(first, second, model["weights"], model["scale"]))
        result[kind] = {"preferred": "left" if delta > 0 else "right" if delta < 0 else "tie",
                        "score_delta": round(delta, 5)}
    response = {"design_principles": result,
                "scope": "advice for comparing variants; no overall UX verdict"}
    if personal:
        learned = {}
        for kind in (*KINDS, "overall"):
            path = personal / f"{kind}.json"
            if not path.is_file() or path.stat().st_size > 100_000:
                continue
            model = json.loads(path.read_text())
            if (model.get("schema") != "virtualos.personal-design/v1"
                    or model.get("dimension") != kind
                    or model.get("metrics", {}).get("promoted") is not True
                    or len(model["weights"]) != len(first)
                    or len(model["scale"]) != len(first)):
                raise ValueError("invalid personal model")
            delta = sum((a - b) * weight / scale for a, b, weight, scale in
                        zip(first, second, model["weights"], model["scale"]))
            learned[kind] = {"preferred": "left" if delta > 0 else "right"
                             if delta < 0 else "tie", "score_delta": round(delta, 5),
                             "source": "local_user_preferences"}
        if learned:
            response["personal_design"] = learned
    return response


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", nargs=2, type=Path, required=True)
    parser.add_argument("--models-dir", type=Path,
                        default=Path(__file__).parent / "models" / "design")
    parser.add_argument("--personal-dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(*args.compare, args.models_dir, args.personal_dir), sort_keys=True))


if __name__ == "__main__":
    main()
