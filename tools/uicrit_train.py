#!/usr/bin/env python3
"""Evaluate a tiny text model of human-written UI critiques, split by screen."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

DATA_SHA256 = "148646535e0dc5c89a6bac44e050a60e209a258f638e90f4578724f77882d8ce"
WORDS = re.compile(r"[a-z][a-z0-9_-]{1,30}")
BINS = 2048
LABELS = ("usability_rating", "aesthetics_rating")


def feature_ids(text: str) -> np.ndarray:
    words = WORDS.findall(text.lower())[:700]
    tokens = set(words) | {f"{a}_{b}" for a, b in zip(words, words[1:])}
    indexes = sorted({int.from_bytes(hashlib.blake2s(token.encode(), digest_size=4).digest(),
                                     "big") % BINS for token in tokens})
    return np.asarray(indexes, dtype=np.int32)


def split(screen: str) -> str:
    value = int.from_bytes(hashlib.sha256(screen.encode()).digest()[:4], "big") % 10
    return "train" if value < 7 else "validation" if value < 8 else "holdout"


def load(path: Path) -> dict[str, list[tuple[np.ndarray, np.ndarray, str]]]:
    if not path.is_file() or path.stat().st_size > 6_000_000:
        raise ValueError("bounded UICrit CSV required")
    if hashlib.sha256(path.read_bytes()).hexdigest() != DATA_SHA256:
        raise ValueError("UICrit checksum mismatch")
    partitions = {name: [] for name in ("train", "validation", "holdout")}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            sources = ast.literal_eval(row["comments_source"])
            comments = ast.literal_eval(row["comments"])
            if (type(sources) is not list or type(comments) is not list
                    or len(sources) > 30 or len(comments) > 30):
                raise ValueError("unexpected critique array")
            if len(sources) != len(comments):
                continue
            human = " ".join(comment for source, comment in zip(sources, comments)
                             if source == "human" and type(comment) is str)
            if not human or len(human) > 20_000:
                continue
            labels = np.asarray([float(row[name]) for name in LABELS], dtype=np.float64)
            if not np.all(np.isfinite(labels)) or np.any(labels < 1) or np.any(labels > 10):
                raise ValueError("invalid human rating")
            partitions[split(row["rico_id"])].append((feature_ids(human), labels,
                                                        row["rico_id"]))
    return partitions


def fit(rows: list[tuple], rate: float, penalty: float) -> tuple[np.ndarray, np.ndarray]:
    mean = np.mean([row[1] for row in rows], axis=0)
    weights = np.zeros((BINS, 2), dtype=np.float64)
    for epoch in range(18):
        step = rate / (1 + epoch * 0.12)
        for indexes, label, _ in rows:
            vector_scale = 1 / math.sqrt(max(1, len(indexes)))
            prediction = mean + weights[indexes].sum(axis=0) * vector_scale
            residual = np.clip(label - prediction, -2, 2)
            weights[indexes] += step * (residual * vector_scale - penalty * weights[indexes])
    return mean, weights


def evaluate(rows: list[tuple], mean: np.ndarray, weights: np.ndarray) -> dict:
    absolute = np.zeros(2)
    baseline = np.zeros(2)
    for indexes, label, _ in rows:
        prediction = np.clip(mean + weights[indexes].sum(axis=0)
                             / math.sqrt(max(1, len(indexes))), 1, 10)
        absolute += np.abs(label - prediction)
        baseline += np.abs(label - mean)
    return {"rows": len(rows), "mae": {name: round(float(value / len(rows)), 4)
                                       for name, value in zip(LABELS, absolute)},
            "mean_baseline_mae": {name: round(float(value / len(rows)), 4)
                                  for name, value in zip(LABELS, baseline)}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    partitions = load(args.train)
    if any(len(rows) < 100 for rows in partitions.values()):
        raise ValueError("insufficient independent screen splits")
    candidates = ((0.02, 0.001), (0.05, 0.001), (0.1, 0.001),
                  (0.05, 0.01), (0.1, 0.01))
    results = []
    for rate, penalty in candidates:
        model = fit(partitions["train"], rate, penalty)
        score = evaluate(partitions["validation"], *model)
        results.append((sum(score["mae"].values()), rate, penalty, model, score))
    _, rate, penalty, (mean, weights), validation = min(results, key=lambda item: item[0])
    holdout = evaluate(partitions["holdout"], mean, weights)
    metrics = {"schema": "virtualos.uicrit-text-metrics/v1", "source":
               "google-research-datasets/uicrit", "license": "cc-by-4.0",
               "data_sha256": DATA_SHA256,
               "train_rows": len(partitions["train"]),
               "train_screens": len({row[2] for row in partitions["train"]}),
               "validation": validation, "holdout": holdout,
               "rate": rate, "penalty": penalty,
               "input": "human critique text, not screenshots",
               "promotable": all(holdout["mae"][name] < holdout["mean_baseline_mae"][name]
                                 for name in LABELS)}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    if metrics["promotable"]:
        artifact = {"schema": "virtualos.uicrit-text/v1", "labels": LABELS,
                    "bins": BINS, "mean": mean.round(6).tolist(),
                    "weights": weights.round(6).tolist(), "data_sha256": DATA_SHA256}
        (args.output / "model.json").write_text(json.dumps(artifact, separators=(",", ":")))
    print(json.dumps(metrics, sort_keys=True))


if __name__ == "__main__":
    main()
