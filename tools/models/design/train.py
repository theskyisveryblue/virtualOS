#!/usr/bin/env python3
"""Small image-feature ranker for human-rated UI alignment preferences.

Train on the pinned GraphicDesignEvaluation alignment shard. Splits whole
source designs, keeping every perturbation of one design in one partition.
No virtualOS private captures enter this model.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import tempfile

import numpy as np
from design_infer import features as runtime_features

REVISION = "a503deef9686671615cbd76b653fff5bbc823fb1"
DATASET = "creative-graphic-design/GraphicDesignEvaluation"
SHARD_HASHES = {
    "alignment": "659928b3e86708652801ab4ea475f428770af7289b7ce631e79adb004bdb7133",
    "overlap": "6c3a0a9d4227d5bfcb0d5d1b7828fefdc73b0cd2fd0f5f0672a5a241411b01a1",
    "whitespace": "c1f6892f338653338e33b47d220b09a8e1e0d14b9922d837dc9d549eba32e390",
}


def image_features(raw: bytes) -> np.ndarray:
    with tempfile.TemporaryDirectory(prefix="vos-design-train-") as directory:
        path = Path(directory) / "sample.png"
        path.write_bytes(raw)
        return np.asarray(runtime_features(path), dtype=np.float32)


def split(group_id: str) -> str:
    value = int.from_bytes(hashlib.sha256(group_id.encode()).digest()[:4], "big") % 10
    return "train" if value < 7 else "validation" if value < 8 else "holdout"


def rows(path: Path, kind: str) -> list[dict]:
    import pyarrow.parquet as pq
    if not path.is_file() or path.stat().st_size > 50_000_000:
        raise ValueError("bounded pinned shard required")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != SHARD_HASHES[kind]:
        raise ValueError("design shard checksum mismatch")
    table = pq.read_table(path)
    if table.num_rows != 400 or set(table.column_names) != {
            "image_id", "image", "perturbation", "scores", "avg"}:
        raise ValueError("unexpected design dataset schema")
    output = []
    for row in table.to_pylist():
        if not 3 <= len(row["scores"]) <= 5 or row["perturbation"] not in range(4):
            raise ValueError("unexpected human rating")
        output.append({"id": row["image_id"], "variant": row["perturbation"],
                       "rating": float(row["avg"]),
                       "features": image_features(row["image"]["bytes"])})
    groups = defaultdict(list)
    for row in output:
        groups[row["id"]].append(row)
    if len(groups) != 100 or any(len(group) != 4 for group in groups.values()):
        raise ValueError("expected 100 four-variant design groups")
    return output


def pairs(items: list[dict], mean: np.ndarray, scale: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    groups = defaultdict(list)
    for row in items:
        groups[row["id"]].append(row)
    x, y = [], []
    for group in groups.values():
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                if abs(left["rating"] - right["rating"]) < 0.5:
                    continue
                difference = (left["features"] - right["features"]) / scale
                x.append(difference)
                y.append(left["rating"] - right["rating"])
    return np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)


def fit(items: list[dict], regularization: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    train_features = np.stack([item["features"] for item in items])
    mean = train_features.mean(axis=0)
    scale = np.maximum(train_features.std(axis=0), 0.03)
    x, y = pairs(items, mean, scale)
    weights = np.linalg.solve(x.T @ x + regularization * np.eye(x.shape[1]), x.T @ y)
    return mean, scale, weights


def evaluate(items: list[dict], scale: np.ndarray, weights: np.ndarray) -> dict:
    x, y = pairs(items, np.zeros_like(scale), scale)
    predicted = x @ weights
    correct = int(np.sum(np.sign(predicted) == np.sign(y)))
    groups = defaultdict(list)
    for item in items:
        groups[item["id"]].append(item)
    top = sum(max(group, key=lambda item: item["features"] @ (weights / scale))["variant"]
              == max(group, key=lambda item: item["rating"])["variant"]
              for group in groups.values())
    return {"design_groups": len(groups), "pairs": len(y), "pair_correct": correct,
            "pair_accuracy": round(correct / len(y), 4),
            "top_variant_correct": top,
            "chance_pair_accuracy": 0.5}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--score", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("LEFT", "RIGHT"))
    parser.add_argument("--models-dir", type=Path,
                        default=Path(__file__).parent / "models" / "design")
    parser.add_argument("--kind", choices=tuple(SHARD_HASHES), default="alignment")
    args = parser.parse_args()
    if args.compare:
        images = []
        for path in args.compare:
            if not path.is_file() or path.stat().st_size > 10_000_000:
                raise ValueError("bounded image file required")
            images.append(image_features(path.read_bytes()))
        result = {}
        for kind in SHARD_HASHES:
            model = json.loads((args.models_dir / f"{kind}.json").read_text())
            if model.get("schema") != "virtualos.design-ranker/v1" or model.get("kind") != kind:
                raise ValueError("invalid design model")
            weights = np.asarray(model["weights"]) / np.asarray(model["scale"])
            difference = float((images[0] - images[1]) @ weights)
            result[kind] = {"preferred": "left" if difference > 0 else "right"
                            if difference < 0 else "tie", "score_delta": round(difference, 5)}
        print(json.dumps({"design_principles": result,
                          "scope": "advice for comparing variants; no overall UX verdict"},
                         sort_keys=True))
        return
    if args.score:
        if not args.model:
            parser.error("--score requires --model")
        model = json.loads(args.model.read_text())
        if model.get("schema") != "virtualos.design-ranker/v1":
            raise ValueError("unsupported model")
        raw = args.score.read_bytes()
        if len(raw) > 10_000_000:
            raise ValueError("image too large")
        score = float(image_features(raw) @
                      (np.asarray(model["weights"]) / np.asarray(model["scale"])))
        print(json.dumps({"relative_score": round(score, 5), "kind": model["kind"],
                          "warning": "Compare variants of the same design only"}))
        return
    if not args.train or not args.output:
        parser.error("--train and --output required")
    expected_path = f"absolute-human-{args.kind}/train-00000-of-00001.parquet"
    if not str(args.train).endswith(expected_path):
        raise ValueError("wrong principle shard")
    data = rows(args.train, args.kind)
    partitions = {name: [row for row in data if split(row["id"]) == name]
                  for name in ("train", "validation", "holdout")}
    candidates = (10.0, 100.0, 1000.0, 10000.0)
    models = {value: fit(partitions["train"], value) for value in candidates}
    validation = {value: evaluate(partitions["validation"], model[1], model[2])
                  for value, model in models.items()}
    selected = max(candidates, key=lambda value: (validation[value]["pair_accuracy"], -value))
    mean, scale, weights = models[selected]
    held_out = evaluate(partitions["holdout"], scale, weights)
    artifact = {"schema": "virtualos.design-ranker/v1", "kind": args.kind,
                "source": {"dataset": DATASET, "revision": REVISION, "license": "apache-2.0",
                           "shard": expected_path, "sha256": SHARD_HASHES[args.kind]},
                "scale": [round(float(x), 6) for x in scale],
                "weights": [round(float(x), 6) for x in weights]}
    metrics = {"schema": "virtualos.design-ranker-metrics/v1",
               "train_groups": len({x["id"] for x in partitions["train"]}),
               "validation": validation[selected], "holdout": held_out,
               "regularization": selected,
               "kind": args.kind,
               "limits": "One design principle; human-rated source graphics, not full UI/UX."}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "model.json").write_text(json.dumps(artifact, separators=(",", ":")))
    (args.output / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    print(json.dumps(metrics, sort_keys=True))


if __name__ == "__main__":
    main()
