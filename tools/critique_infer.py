#!/usr/bin/env python3
"""Dependency-free inference for a human-critique rating model."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import math
import re

WORDS = re.compile(r"[a-z][a-z0-9_-]{1,30}")


def feature_ids(text: str, bins: int) -> list[int]:
    words = WORDS.findall(text.lower())[:700]
    tokens = set(words) | {f"{a}_{b}" for a, b in zip(words, words[1:])}
    return sorted({int.from_bytes(hashlib.blake2s(token.encode(), digest_size=4).digest(),
                                  "big") % bins for token in tokens})


def score(model: dict, text: str) -> dict:
    if (model.get("schema") != "virtualos.uicrit-text/v1"
            or model.get("labels") != ["usability_rating", "aesthetics_rating"]
            or model.get("bins") != 2048 or len(model.get("weights", [])) != 2048):
        raise ValueError("unsupported critique model")
    indexes = feature_ids(text, model["bins"])
    if len(indexes) < 5:
        return {"critique_model": "abstain", "reason": "too_few_features"}
    prediction = [model["mean"][i] + sum(model["weights"][index][i]
                                           for index in indexes) / math.sqrt(len(indexes))
                  for i in range(2)]
    return {"critique_model": "advice_only",
            "predicted_ratings": {label: round(max(1, min(10, value)), 3)
                                  for label, value in zip(model["labels"], prediction)},
            "scope": "human-written critique text; not a screenshot or overall UI verdict"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    if not args.score.is_file() or args.score.stat().st_size > 20_000:
        raise ValueError("bounded critique text file required")
    text = args.score.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("empty critique")
    model = json.loads(args.model.read_text())
    print(json.dumps(score(model, text), sort_keys=True))


if __name__ == "__main__":
    main()
