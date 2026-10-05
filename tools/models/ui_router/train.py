#!/usr/bin/env python3
"""Train a compact interface-route classifier on a pinned public dataset.

Works locally or in Hugging Face Jobs with only Python's standard library.
It never reads virtualOS's private trace journal. The trained model recommends
an interface mode; it cannot perform an action.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import urllib.request

DATASET = "akashnaren/agent-ui-human"
REVISION = "bff66198177024eeed2a4914cc3df03adc99fe35"
HASHES = {
    "train": "760e0a0de25552678415b933e9c1572848c6ce7f50ff6335c7cf2dae14277a50",
    "test": "344cb988489fd33449ac798e74f0a6f5f835e325a4245c85ef111c82cfcef4d1",
}
CLASSES = ("cli", "structured_api", "dom_click", "form")
TOKEN = re.compile(r"[a-z][a-z0-9_-]{1,32}")
MAX_BYTES = 100_000


def fetch(split: str, source: Path | None) -> list[dict]:
    if source:
        payload = (source / f"{split}.jsonl").read_bytes()
    else:
        url = f"https://huggingface.co/datasets/{DATASET}/resolve/{REVISION}/{split}.jsonl"
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES or hashlib.sha256(payload).hexdigest() != HASHES[split]:
        raise ValueError(f"{split} revision or checksum changed")
    rows = [json.loads(line) for line in payload.splitlines() if line.strip()]
    if len(rows) != {"train": 50, "test": 10}[split]:
        raise ValueError("unexpected row count")
    for row in rows:
        if (set(row) != {"id", "preferred_ui", "domain", "prompt", "rationale"}
                or row["preferred_ui"] not in CLASSES
                or not isinstance(row["prompt"], str) or len(row["prompt"]) > 1000):
            raise ValueError("unexpected dataset schema")
    return rows


def features(prompt: str) -> set[str]:
    words = TOKEN.findall(prompt.lower())[:80]
    return {f"w:{word}" for word in words} | {
        f"b:{left}_{right}" for left, right in zip(words, words[1:])}


def fit(rows: list[dict], alpha: float) -> dict:
    labels = Counter(row["preferred_ui"] for row in rows)
    counts = {name: Counter() for name in CLASSES}
    vocabulary = set()
    for row in rows:
        tokens = features(row["prompt"])
        vocabulary.update(tokens)
        counts[row["preferred_ui"]].update(tokens)
    weights = {}
    for token in sorted(vocabulary):
        weights[token] = [round(math.log((counts[name][token] + alpha)
                                          / (labels[name] + 2 * alpha)), 7)
                          for name in CLASSES]
    return {"schema": "virtualos.hf-ui-router/v1", "classes": list(CLASSES),
            "priors": [round(math.log((labels[name] + 1) / (len(rows) + len(CLASSES))), 7)
                       for name in CLASSES], "weights": weights,
            "unknown_logprob": [round(math.log(alpha / (labels[name] + 2 * alpha)), 7)
                                for name in CLASSES],
            "source": {"dataset": DATASET, "revision": REVISION, "license": "apache-2.0",
                       "train_sha256": HASHES["train"], "rows": len(rows)}}


def predict(model: dict, prompt: str) -> dict:
    tokens = features(prompt)
    known = tokens & model["weights"].keys()
    scores = [model["priors"][i] + sum(model["weights"][token][i] for token in known)
              for i in range(len(CLASSES))]
    peak = max(scores)
    exp = [math.exp(score - peak) for score in scores]
    probs = [value / sum(exp) for value in exp]
    best = max(range(len(CLASSES)), key=lambda index: probs[index])
    return {"choice": CLASSES[best], "confidence": round(probs[best], 5),
            "known_features": len(known)}


def evaluate(model: dict, rows: list[dict]) -> dict:
    predictions = [predict(model, row["prompt"]) for row in rows]
    correct = sum(prediction["choice"] == row["preferred_ui"]
                  for prediction, row in zip(predictions, rows))
    baseline = Counter(row["preferred_ui"] for row in rows)
    return {"count": len(rows), "correct": correct,
            "accuracy": round(correct / len(rows), 4),
            "majority_baseline": max(baseline.values()),
            "coverage_at_0_55": sum(p["confidence"] >= 0.55 and p["known_features"] >= 2
                                    for p in predictions),
            "correct_at_0_55": sum(p["confidence"] >= 0.55 and p["known_features"] >= 2
                                   and p["choice"] == row["preferred_ui"]
                                   for p, row in zip(predictions, rows)),
            "errors": [{"id": row["id"], "expected": row["preferred_ui"],
                        "predicted": prediction["choice"]}
                       for p, row in zip(predictions, rows)
                       if (prediction := p)["choice"] != row["preferred_ui"]]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, help="directory with pinned train/test files")
    parser.add_argument("--output", type=Path, default=Path("/tmp/virtualos-hf-ui-router"))
    parser.add_argument("--publish-private", help="Hugging Face model repo, e.g. owner/name")
    parser.add_argument("--suggest", help="local text to route; advice only")
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()
    if args.suggest is not None:
        if not args.model or len(args.suggest) > 1000:
            raise ValueError("bounded prompt and model required")
        model = json.loads(args.model.read_text())
        if model.get("schema") != "virtualos.hf-ui-router/v1":
            raise ValueError("unsupported router model")
        prediction = predict(model, args.suggest)
        feature_count = len(features(args.suggest))
        ratio = prediction["known_features"] / max(1, feature_count)
        prediction["known_feature_ratio"] = round(ratio, 4)
        prediction["status"] = "advice_only" if ratio >= 0.35 else "out_of_domain"
        print(json.dumps(prediction, sort_keys=True))
        return
    train = fetch("train", args.source)
    test = fetch("test", args.source)
    overlap = {row["id"] for row in train} & {row["id"] for row in test}
    if overlap:
        raise ValueError("train/test IDs overlap")
    # Select smoothing by leave-one-out on train; test remains untouched.
    candidates = (0.25, 0.5, 1.0, 2.0)
    scores = {alpha: sum(predict(fit(train[:index] + train[index + 1:], alpha),
                                 row["prompt"])["choice"] == row["preferred_ui"]
                         for index, row in enumerate(train)) for alpha in candidates}
    alpha = max(candidates, key=lambda value: (scores[value], -value))
    model = fit(train, alpha)
    metrics = {"schema": "virtualos.hf-ui-router-metrics/v1", "train_rows": len(train),
               "test_rows": len(test), "alpha": alpha, "leave_one_out_correct": scores[alpha],
               "test": evaluate(model, test), "model_bytes": 0,
               "limitations": "Tool-mode routing only; no visual design judgment or Xcode navigation."}
    args.output.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
    metrics["model_bytes"] = len(payload)
    (args.output / "model.json").write_bytes(payload)
    (args.output / "metrics.json").write_text(json.dumps(metrics, sort_keys=True, indent=2) + "\n")
    if args.publish_private:
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9._-]+", args.publish_private):
            raise ValueError("invalid model repository")
        if metrics["test"]["correct"] <= metrics["test"]["majority_baseline"]:
            raise ValueError("test result does not beat majority baseline")
        from huggingface_hub import HfApi
        card = ("---\nlicense: apache-2.0\ntags:\n- computer-use\n- tool-routing\n"
                "- virtualos\n---\n# virtualOS compact interface router\n\n"
                "A 46 KB text-only Naive Bayes router trained on 50 human-written prompts "
                "from [Agent UI Human](https://huggingface.co/datasets/akashnaren/agent-ui-human) "
                f"at revision `{REVISION}` (Apache 2.0). The test set has 10 prompts. "
                f"It scored {metrics['test']['correct']}/10 versus a "
                f"{metrics['test']['majority_baseline']}/10 majority baseline. "
                "It chooses among CLI, structured API, DOM click, and form. "
                "It has not learned visual design quality, general software navigation, "
                "or Xcode operation. Use as advice only; verify actions separately.\n\n"
                "`model.json` is a virtualOS specific count model, not a Transformers checkpoint. "
                "`metrics.json` contains the full held-out result.\n")
        (args.output / "README.md").write_text(card)
        (args.output / "train.py").write_text(Path(__file__).read_text())
        api = HfApi()
        api.create_repo(args.publish_private, private=True, exist_ok=True)
        api.upload_folder(folder_path=str(args.output), repo_id=args.publish_private,
                          commit_message="Train compact UI router on pinned Apache dataset")
        print(json.dumps({"private_model_repo": args.publish_private,
                          "uploaded_files": ["README.md", "train.py", "metrics.json", "model.json"]}))
    print(json.dumps(metrics, sort_keys=True))


if __name__ == "__main__":
    main()
