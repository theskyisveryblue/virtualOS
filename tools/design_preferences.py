#!/usr/bin/env python3
"""Local-only, grouped human preference feedback for the design rankers."""

from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

from design_infer import features

SCHEMA = "virtualos.design-preference/v1"
SYMBOL = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")
DIMENSIONS = ("alignment", "overlap", "whitespace", "overall")
MAX_JOURNAL = 2_000_000


def source_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    if path.stat().st_size > MAX_JOURNAL:
        raise ValueError("preference journal full")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    seen = set()
    for row in rows:
        if (set(row) != {"schema", "family", "dimension", "winner", "left_sha256",
                         "right_sha256", "delta", "observed_at"}
                or row["schema"] != SCHEMA or row["dimension"] not in DIMENSIONS
                or row["winner"] not in {"left", "right"}
                or type(row["family"]) is not str or not SYMBOL.fullmatch(row["family"])
                or type(row["observed_at"]) is not int
                or type(row["delta"]) is not list or len(row["delta"]) != 466
                or any(type(value) not in (float, int) or not math.isfinite(value)
                       or abs(value) > 1 for value in row["delta"])
                or any(type(row[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", row[key])
                       for key in ("left_sha256", "right_sha256"))):
            raise ValueError("invalid preference journal")
        key = (row["family"], row["dimension"],
               tuple(sorted((row["left_sha256"], row["right_sha256"]))))
        if key in seen:
            raise ValueError("duplicate preference pair")
        seen.add(key)
    return rows


def record(path: Path, left: Path, right: Path, winner: str,
           dimension: str, family: str) -> dict:
    if not SYMBOL.fullmatch(family) or winner not in {"left", "right"}:
        raise ValueError("explicit family and left/right human choice required")
    if dimension not in DIMENSIONS:
        raise ValueError("unsupported design dimension")
    for image in (left, right):
        if not image.is_file() or image.stat().st_size > 10_000_000:
            raise ValueError("bounded image file required")
    left_hash = hashlib.sha256(left.read_bytes()).hexdigest()
    right_hash = hashlib.sha256(right.read_bytes()).hexdigest()
    if left_hash == right_hash:
        raise ValueError("same image on both sides")
    first, second = features(left), features(right)
    row = {"schema": SCHEMA, "family": family, "dimension": dimension,
           "winner": winner, "left_sha256": left_hash, "right_sha256": right_hash,
           "delta": [round(a - b, 6) for a, b in zip(first, second)],
           "observed_at": int(time.time())}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        existing = handle.read(MAX_JOURNAL + 1)
        if len(existing) > MAX_JOURNAL:
            raise ValueError("preference journal full")
        for line in existing.splitlines():
            if line.strip():
                previous = json.loads(line)
                if (previous.get("family") == family and previous.get("dimension") == dimension
                        and {previous.get("left_sha256"), previous.get("right_sha256")}
                        == {left_hash, right_hash}):
                    raise ValueError("preference pair already recorded")
        handle.seek(0, os.SEEK_END)
        if handle.tell() + len(json.dumps(row)) + 1 > MAX_JOURNAL:
            raise ValueError("preference journal full")
        handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)
    return {"preference": "recorded", "dimension": dimension, "family": family,
            "pixels_retained": False, "cloud_upload": False}


def normalized_delta(row: dict, scale: list[float]) -> list[float]:
    return [value / divisor for value, divisor in zip(row["delta"], scale)]


def accuracy(rows: list[dict], weights: list[float], scale: list[float]) -> dict:
    correct = sum((sum(x * w for x, w in zip(normalized_delta(row, scale), weights)) > 0)
                  == (row["winner"] == "left") for row in rows)
    return {"rows": len(rows), "correct": correct,
            "accuracy": round(correct / len(rows), 4) if rows else 0}


def train(journal: Path, models_dir: Path, output: Path, dimension: str) -> dict:
    rows = [row for row in source_rows(journal) if row["dimension"] == dimension]
    families = {row["family"] for row in rows}
    if len(rows) < 50 or len(families) < 10:
        return {"personal_model": "insufficient_feedback", "rows": len(rows),
                "families": len(families), "minimum_rows": 50, "minimum_families": 10}
    roles = {family: ("train" if index % 5 < 3 else "validation" if index % 5 == 3
                      else "holdout")
             for index, family in enumerate(sorted(families,
                                                    key=lambda x: hashlib.sha256(x.encode()).digest()))}
    partitions = {role: [row for row in rows if roles[row["family"]] == role]
                  for role in ("train", "validation", "holdout")}
    if any(not values for values in partitions.values()):
        raise ValueError("independent family split unavailable")
    if len(partitions["holdout"]) < 10:
        return {"personal_model": "insufficient_holdout", "rows": len(rows),
                "holdout_rows": len(partitions["holdout"]), "minimum_holdout_rows": 10}
    base = (json.loads((models_dir / f"{dimension}.json").read_text())
            if dimension != "overall" else None)
    size = len(rows[0]["delta"])
    scale = base["scale"] if base else [1.0] * size
    prior = base["weights"] if base else [0.0] * size
    if len(scale) != size or len(prior) != size:
        raise ValueError("model feature length mismatch")
    weight = prior.copy()
    for epoch in range(40):
        rate = 0.02 / (1 + epoch * 0.1)
        for row in partitions["train"]:
            vector = normalized_delta(row, scale)
            label = 1 if row["winner"] == "left" else -1
            score = max(-30, min(30, sum(a * b for a, b in zip(weight, vector))))
            gradient = label / (1 + math.exp(label * score))
            weight = [w + rate * (gradient * x - 0.05 * (w - p))
                      for w, x, p in zip(weight, vector, prior)]
    baseline = accuracy(partitions["holdout"], prior, scale)
    personalized = accuracy(partitions["holdout"], weight, scale)
    label_counts = Counter(row["winner"] for row in partitions["holdout"])
    majority_correct = max(label_counts.values())
    required_correct = max(baseline["correct"], majority_correct)
    metrics = {"schema": "virtualos.personal-design-metrics/v1",
               "dimension": dimension, "rows": len(rows), "families": len(families),
               "train_rows": len(partitions["train"]),
               "validation": accuracy(partitions["validation"], weight, scale),
               "holdout": personalized, "base_holdout": baseline,
               "majority_holdout_correct": majority_correct,
               "promoted": (personalized["correct"] >= required_correct + 2
                            and (personalized["correct"] - required_correct)
                            / len(partitions["holdout"]) >= 0.1)}
    if not metrics["promoted"]:
        if output.is_file():
            os.replace(output, output.with_suffix(".stale.json"))
        return metrics
    artifact = {"schema": "virtualos.personal-design/v1", "dimension": dimension,
                "scale": scale, "weights": [round(value, 6) for value in weight],
                "journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
                "metrics": metrics}
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=output.parent, delete=False,
                                     encoding="utf-8", prefix=".personal-design-") as handle:
        json.dump(artifact, handle, sort_keys=True, separators=(",", ":"))
        temp = Path(handle.name)
    os.replace(temp, output)
    output.chmod(0o600)
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("record", "status", "train"))
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--left", type=Path)
    parser.add_argument("--right", type=Path)
    parser.add_argument("--winner", choices=("left", "right"))
    parser.add_argument("--dimension", choices=DIMENSIONS)
    parser.add_argument("--family")
    args = parser.parse_args()
    if args.command == "record":
        if not all((args.left, args.right, args.winner, args.dimension, args.family)):
            parser.error("record requires left, right, winner, dimension, and family")
        result = record(args.journal, args.left, args.right, args.winner,
                        args.dimension, args.family)
    elif args.command == "train":
        if not args.dimension or not args.output:
            parser.error("train requires dimension and output")
        result = train(args.journal, args.models_dir, args.output, args.dimension)
    else:
        rows = source_rows(args.journal)
        result = {"preference_rows": len(rows),
                  "dimensions": dict(Counter(row["dimension"] for row in rows)),
                  "families": len({row["family"] for row in rows}),
                  "pixels_retained": False, "cloud_upload": False}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
