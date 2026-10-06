#!/usr/bin/env python3
"""Bounded symbolic choice model. It never executes the chosen action.

Training accepts only independently reviewed, rights-cleared observations.
Synthetic fixtures exercise the math without producing a deployable model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

SCHEMA = "virtualos.decision-examples/v1"
MODEL_SCHEMA = "virtualos.small-decider/v1"
SYMBOL = re.compile(r"^[a-z][a-z0-9_-]*(?:[.][a-z0-9_-]+)*$")
MAX_INPUT = 1_048_576
MAX_MODEL = 32_768


def symbol(value: object) -> str:
    if type(value) is not str or len(value) > 64 or not SYMBOL.fullmatch(value):
        raise ValueError("unreviewed or oversized symbol")
    return value


def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def exact(value: object, fields: set[str]) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise ValueError("missing or unexpected field")
    return value


def validate_example(value: object, synthetic: bool = False) -> dict:
    row = exact(value, {"state", "options", "chosen", "split", "group_id",
                        "rights", "source", "review_id", "verified"})
    state = exact(row["state"], {"goal", "facts"})
    symbol(state["goal"])
    facts = state["facts"]
    if type(facts) is not list or len(facts) > 8:
        raise ValueError("facts must be at most eight distinct symbols")
    for fact in facts:
        symbol(fact)
    if len(facts) != len(set(facts)):
        raise ValueError("duplicate facts")
    options = row["options"]
    if type(options) is not list or not 2 <= len(options) <= 9:
        raise ValueError("options must be 2-9 distinct symbols")
    for option in options:
        symbol(option)
    if len(options) != len(set(options)):
        raise ValueError("duplicate options")
    if "abstain" not in options:
        raise ValueError("abstain option required")
    if (type(row["chosen"]) is not str or row["chosen"] not in options
            or type(row["split"]) is not str
            or row["split"] not in {"train", "validation", "holdout"}):
        raise ValueError("invalid choice or split")
    symbol(row["group_id"])
    symbol(row["review_id"])
    if row["verified"] is not True:
        raise ValueError("independent outcome verification required")
    rights = exact(row["rights"], {"ownership", "training", "consent_id"})
    if (type(rights["ownership"]) is not str or rights["ownership"] not in {"owned", "licensed"}
            or rights["training"] != "allowed"):
        raise ValueError("training rights required")
    symbol(rights["consent_id"])
    source = exact(row["source"], {"kind", "evidence", "sha256"})
    if type(source["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
        raise ValueError("source evidence digest required")
    if synthetic:
        if source["kind"] != "declared-fixture" or source["evidence"] != "declared":
            raise ValueError("synthetic test requires declared fixture")
    elif source["kind"] != "virtualos-episode" or source["evidence"] != "observed":
        raise ValueError("training requires observed episode evidence")
    return row


def validate_dataset(value: object, synthetic: bool = False) -> list[dict]:
    dataset = exact(value, {"schema", "examples"})
    if dataset["schema"] != SCHEMA or type(dataset["examples"]) is not list:
        raise ValueError("unsupported dataset")
    rows = dataset["examples"]
    if not 3 <= len(rows) <= 2048:
        raise ValueError("dataset must contain 3-2048 examples")
    groups: dict[str, str] = {}
    evidence: dict[str, str] = {}
    reviews: set[str] = set()
    splits: set[str] = set()
    for item in rows:
        row = validate_example(item, synthetic)
        group, split = row["group_id"], row["split"]
        if group in groups and groups[group] != split:
            raise ValueError("related group crosses splits")
        digest = row["source"]["sha256"]
        if digest in evidence and evidence[digest] != split:
            raise ValueError("shared evidence crosses splits")
        if row["review_id"] in reviews:
            raise ValueError("duplicate review ID")
        groups[group] = split
        evidence[digest] = split
        reviews.add(row["review_id"])
        splits.add(split)
    if splits != {"train", "validation", "holdout"} or len(groups) < 3:
        raise ValueError("independent train, validation, and holdout groups required")
    return rows


def features(state: dict, option: str) -> list[str]:
    # Per-choice symbolic weights; no page text, pixels, or large embedding.
    return [f"{option}|bias", f"{option}|goal:{state['goal']}"] + [
        f"{option}|fact:{fact}" for fact in sorted(state["facts"])]


def score(weights: dict[str, float], state: dict, options: list[str]) -> dict[str, float]:
    raw = {option: sum(weights.get(feature, 0.0) for feature in features(state, option))
           for option in options}
    peak = max(raw.values())
    exp = {option: math.exp(value - peak) for option, value in raw.items()}
    total = sum(exp.values())
    return {option: round(exp[option] / total, 6) for option in options}


def train(rows: list[dict]) -> dict:
    weights: dict[str, float] = {}
    examples = sorted((row for row in rows if row["split"] == "train"),
                      key=lambda row: (row["group_id"], row["review_id"]))
    for _ in range(60):
        for row in examples:
            state, options = row["state"], row["options"]
            probabilities = score(weights, state, options)
            for option in options:
                delta = 0.15 * ((1.0 if option == row["chosen"] else 0.0)
                                - probabilities[option])
                for feature in features(state, option):
                    weights[feature] = round(weights.get(feature, 0.0) + delta, 6)
    if len(weights) > 512:
        raise ValueError("model exceeds feature limit")
    allowed = sorted({option for row in examples for option in row["options"]})
    return {"schema": MODEL_SCHEMA, "weights": weights,
            "allowed_options": allowed, "confidence_floor": 0.8}


def decide(model: dict, state: dict, options: list[str]) -> dict:
    probabilities = score(model["weights"], state, options)
    selected = max(sorted(options), key=lambda option: probabilities[option])
    known = (any(f"{option}|goal:{state['goal']}" in model["weights"] for option in options)
             and all(any(f"{option}|fact:{fact}" in model["weights"] for option in options)
                     for fact in state["facts"]))
    return {"choice": selected if known and probabilities[selected] >= model["confidence_floor"] else "abstain",
            "probabilities": probabilities, "execution": "disabled"}


def evaluate(model: dict, rows: list[dict], split: str) -> dict:
    subset = [row for row in rows if row["split"] == split]
    accepted = [decide(model, row["state"], row["options"])["choice"] for row in subset]
    return {"count": len(subset), "correct": sum(choice == row["chosen"] for choice, row in zip(accepted, subset)),
            "abstained": accepted.count("abstain")}


def fixture() -> dict:
    rows = []
    for split, group in (("train", "fixture.train"), ("validation", "fixture.validation"),
                         ("holdout", "fixture.holdout")):
        for status in ("false", "true", "error"):
            rows.append({"state": {"goal": "finder_window_present", "facts": [f"ax.finder_window.{status}"]},
                         "options": ["dock_finder_click", "abstain"],
                         "chosen": "dock_finder_click" if status == "false" else "abstain",
                         "split": split, "group_id": group,
                         "rights": {"ownership": "owned", "training": "allowed", "consent_id": "fixture"},
                         "source": {"kind": "declared-fixture", "evidence": "declared",
                                    "sha256": hashlib.sha256(f"{split}:{status}".encode()).hexdigest()},
                         "review_id": f"fixture.{split}.{status}", "verified": True})
    return {"schema": SCHEMA, "examples": rows}


def clef_fixture() -> dict:
    """Public synthetic request using Cloudflare's typed-choice input shape."""
    return {"model": "clef-flash",
            "state": {"goal": "finder_window_present", "facts": ["ax.finder_window.false"]},
            "questions": {"next_action": {
                "type": "choice",
                "instructions": "Choose the safest next action for the stated symbolic goal and observed fact.",
                "criteria": {
                    "dock_finder_click": "Open a Finder window with its Dock icon when no Finder window is present.",
                    "abstain": "Take no action when the observation is uncertain or the goal is already met."
                }}}}


def read_json(path: Path, limit: int) -> object:
    if path.stat().st_size > limit:
        raise ValueError("input exceeds byte limit")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("self-test", "train", "decide", "clef-fixture"))
    parser.add_argument("input", nargs="?")
    parser.add_argument("output", nargs="?")
    args = parser.parse_args()
    try:
        if args.command == "clef-fixture":
            if args.input or args.output:
                raise ValueError("clef fixture accepts no files")
            print(json.dumps(clef_fixture(), sort_keys=True, separators=(",", ":")))
            return 0
        if args.command == "self-test":
            rows = validate_dataset(fixture(), synthetic=True)
            model = train(rows)
            data = json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
            assert len(data) < MAX_MODEL
            assert evaluate(model, rows, "holdout")["correct"] == 3
            assert decide(model, {"goal": "finder_window_present", "facts": ["ax.finder_window.unknown"]},
                          ["dock_finder_click", "abstain"])["choice"] == "abstain"
            assert set(clef_fixture()["questions"]["next_action"]["criteria"]) == {
                "dock_finder_click", "abstain"}
            for bad in ("training", "verified", "group_id", "shared_evidence"):
                tampered = fixture()
                row = tampered["examples"][0]
                if bad == "training": row["rights"][bad] = "unknown"
                elif bad == "verified": row[bad] = False
                elif bad == "shared_evidence": row["source"]["sha256"] = tampered["examples"][-1]["source"]["sha256"]
                else: row[bad] = "fixture.holdout"
                try: validate_dataset(tampered, synthetic=True)
                except ValueError: pass
                else: raise AssertionError(f"accepted invalid {bad}")
            print(json.dumps({"small_decider": "passed", "model_bytes": len(data),
                              "holdout": evaluate(model, rows, "holdout"),
                              "finder_false": decide(model, rows[0]["state"], rows[0]["options"]),
                              "source": "synthetic-only"},
                             sort_keys=True))
            return 0
        if not args.input or not args.output:
            raise ValueError("input and output files required")
        if args.command == "train":
            rows = validate_dataset(read_json(Path(args.input), MAX_INPUT))
            model = train(rows)
            data = json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
            if len(data) > MAX_MODEL:
                raise ValueError("model exceeds byte limit")
            # The holdout is reported once. Training never claims generality.
            report = {"validation": evaluate(model, rows, "validation"),
                      "holdout": evaluate(model, rows, "holdout"), "model_bytes": len(data)}
            Path(args.output).write_bytes(data)
            print(json.dumps(report, sort_keys=True))
            return 0
        model = exact(read_json(Path(args.input), MAX_MODEL),
                      {"schema", "weights", "allowed_options", "confidence_floor"})
        if model["schema"] != MODEL_SCHEMA or type(model["weights"]) is not dict:
            raise ValueError("unsupported model")
        if len(model["weights"]) > 512 or any(type(k) is not str or len(k) > 160
                                               or type(v) not in (int, float) or not math.isfinite(v)
                                               or abs(v) > 100 for k, v in model["weights"].items()):
            raise ValueError("invalid weights")
        if (type(model["confidence_floor"]) not in (int, float)
                or not 0.5 <= model["confidence_floor"] <= 1):
            raise ValueError("invalid confidence floor")
        allowed = model["allowed_options"]
        if type(allowed) is not list or not 2 <= len(allowed) <= 9:
            raise ValueError("invalid allowed options")
        for option in allowed: symbol(option)
        if "abstain" not in allowed or len(allowed) != len(set(allowed)):
            raise ValueError("invalid allowed options")
        request = exact(read_json(Path(args.output), 4096), {"state", "options"})
        state = exact(request["state"], {"goal", "facts"})
        symbol(state["goal"])
        if type(state["facts"]) is not list or len(state["facts"]) > 8:
            raise ValueError("invalid facts")
        for fact in state["facts"]: symbol(fact)
        if len(state["facts"]) != len(set(state["facts"])):
            raise ValueError("duplicate facts")
        if type(request["options"]) is not list or not 2 <= len(request["options"]) <= 9:
            raise ValueError("invalid options")
        for option in request["options"]: symbol(option)
        if len(request["options"]) != len(set(request["options"])):
            raise ValueError("duplicate options")
        if "abstain" not in request["options"] or not set(request["options"]) <= set(allowed):
            raise ValueError("unreviewed choice options")
        print(json.dumps(decide(model, state, request["options"]), sort_keys=True))
        return 0
    except (AssertionError, ValueError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"small_decider=refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
