#!/usr/bin/env python3
"""Train and inspect a tiny local choice model from verified local feedback.

The model is shadow-only until enough independent app/site groups exist.
Neither training nor inference sends data to a provider or executes an action.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

from offline_policy import SCHEMA as REQUEST_SCHEMA, SOURCE_APPS, open_db, suggest as count_suggest
from offline_policy import validate_context, validate_options
from small_decider import decide, evaluate, symbol, train, unique_pairs

DATASET_SCHEMA = "virtualos.local-feedback/v1"
ARTIFACT_SCHEMA = "virtualos.offline-linear/v1"
MAX_DATASET = 1_048_576
MAX_ARTIFACT = 32_768


def read_json(path: Path, limit: int) -> dict:
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("bounded input file required")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    if type(value) is not dict:
        raise ValueError("JSON object required")
    return value


def training_rows(dataset: dict) -> tuple[list[dict], int]:
    if set(dataset) != {"schema", "usage", "examples"} or dataset["schema"] != DATASET_SCHEMA:
        raise ValueError("unsupported local dataset")
    if dataset["usage"] != "local-only" or type(dataset["examples"]) is not list:
        raise ValueError("local-only examples required")
    examples = dataset["examples"]
    if not 3 <= len(examples) <= 4096:
        raise ValueError("3-4096 examples required")
    validated = []
    groups = set()
    reviews = set()
    for item in examples:
        if (type(item) is not dict or not {"review_id", "context", "action", "outcome"} <= set(item)
                or set(item) - {"review_id", "context", "action", "outcome", "locator", "source_app"}):
            raise ValueError("unexpected local feedback fields")
        review = item["review_id"]
        if type(review) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,95}", review):
            raise ValueError("invalid review ID")
        if review in reviews:
            raise ValueError("duplicate review ID")
        reviews.add(review)
        context = validate_context(item["context"])
        action = symbol(item["action"])
        if (action == "abstain" or type(item["outcome"]) is not str
                or item["outcome"] not in {"verified_success", "verified_failure"}):
            raise ValueError("invalid verified outcome")
        if "locator" in item:
            locator = item["locator"]
            if (type(locator) is not dict or set(locator) != {"x", "y"}
                    or type(locator["x"]) not in (int, float)
                    or type(locator["y"]) not in (int, float)
                    or not 0 <= locator["x"] < 1 or not 0.8 <= locator["y"] < 1):
                    raise ValueError("invalid normalized locator")
        if ("source_app" in item and
                (type(item["source_app"]) is not str or item["source_app"] not in SOURCE_APPS)):
            raise ValueError("unreviewed source app")
        scope = context["scope"]
        group = scope["kind"] + "\0" + scope["id"]
        groups.add(group)
        validated.append((item, context, action, group))
    if len(groups) < 3:
        raise ValueError("at least three independent scope groups required")
    ordered = sorted(groups, key=lambda group: hashlib.sha256(group.encode()).digest())
    roles = {group: ("holdout" if index == len(ordered) - 1 else
                     "validation" if index == len(ordered) - 2 else "train")
             for index, group in enumerate(ordered)}
    rows = []
    for item, context, action, group in validated:
        rows.append({"state": {"goal": context["goal"], "facts": context["facts"]},
                     "options": [action, "abstain"],
                     "chosen": action if item["outcome"] == "verified_success" else "abstain",
                     "split": roles[group], "group_id": hashlib.sha256(group.encode()).hexdigest()[:20],
                     "review_id": item["review_id"]})
    return rows, len(groups)


def metrics(model: dict, rows: list[dict], split: str) -> dict:
    result = evaluate(model, rows, split)
    result["always_abstain_correct"] = sum(row["chosen"] == "abstain"
                                           for row in rows if row["split"] == split)
    return result


def ambiguity_report(rows: list[dict]) -> dict:
    """Count outcomes the current symbolic input cannot distinguish."""
    buckets = defaultdict(Counter)
    for row in rows:
        key = (row["state"]["goal"], tuple(sorted(row["state"]["facts"])),
               tuple(sorted(row["options"])))
        buckets[key][row["chosen"]] += 1
    conflicted = [counts for counts in buckets.values() if len(counts) > 1]
    return {"distinct_states": len(buckets),
            "conflicting_states": len(conflicted),
            "rows_in_conflicting_states": sum(sum(counts.values()) for counts in conflicted)}


def compare_source_app(dataset_path: Path) -> dict:
    """Evaluate a source-app feature on grouped splits without saving a model."""
    dataset = read_json(dataset_path, MAX_DATASET)
    rows, _ = training_rows(dataset)
    source_app = {}
    allowed = {"Finder": "finder", "Terminal": "terminal", "Safari": "safari",
               "Google Chrome": "chrome"}
    for item in dataset["examples"]:
        source_app[item["review_id"]] = allowed.get(item.get("source_app"), "unknown")
    variants = {}
    for name in ("base", "source_app"):
        transformed = json.loads(json.dumps(rows))
        if name == "source_app":
            for row in transformed:
                if "dock_app_click" in row["options"]:
                    row["state"]["facts"].append("frontmost." + source_app.get(row["review_id"], "unknown"))
        model = train(transformed)
        variants[name] = {"ambiguity": ambiguity_report(transformed),
                          "validation": metrics(model, transformed, "validation"),
                          "holdout": metrics(model, transformed, "holdout")}
    return {"offline_comparison": "local_metadata_only", "variants": variants}


def train_local(dataset_path: Path, model_path: Path) -> dict:
    dataset = read_json(dataset_path, MAX_DATASET)
    rows, groups = training_rows(dataset)
    model = train(rows)
    validation = metrics(model, rows, "validation")
    holdout = metrics(model, rows, "holdout")
    # Grouped holdout is intentionally decisive. Current narrow Dock corpus
    # cannot promote this general model into the live action path.
    eligible = (groups >= 10 and validation["count"] >= 10 and holdout["count"] >= 10
                and validation["correct"] > validation["always_abstain_correct"]
                and holdout["correct"] > holdout["always_abstain_correct"])
    artifact = {"schema": ARTIFACT_SCHEMA, "mode": "candidate" if eligible else "shadow",
                "training_rows": len(rows), "scope_groups": groups,
                "ambiguity": ambiguity_report(rows),
                "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                "validation": validation, "holdout": holdout, "model": model}
    payload = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > MAX_ARTIFACT:
        raise ValueError("model artifact exceeds 32 KiB")
    temp = model_path.with_name(model_path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, model_path)
    finally:
        if temp.exists():
            temp.unlink()
    return {"offline_model": "trained", "mode": artifact["mode"],
            "artifact_bytes": len(payload), "training_rows": len(rows),
            "scope_groups": groups, "ambiguity": artifact["ambiguity"],
            "validation": validation, "holdout": holdout}


def suggest_all(request: dict, db_path: Path, model_path: Path) -> dict:
    if (type(request) is not dict or set(request) not in
            ({"schema", "context", "options"},
             {"schema", "context", "options", "source_app"})
            or request["schema"] != REQUEST_SCHEMA):
        raise ValueError("unsupported symbolic request")
    context = validate_context(request["context"])
    options = validate_options(request["options"])
    connection = open_db(db_path)
    try:
        count = count_suggest(connection, request)
    finally:
        connection.close()
    output = {"count_policy": count, "linear_policy": None, "execution": "disabled"}
    if not model_path.is_file():
        return output
    artifact = read_json(model_path, MAX_ARTIFACT)
    if artifact.get("schema") != ARTIFACT_SCHEMA or artifact.get("mode") not in {"shadow", "candidate"}:
        raise ValueError("invalid local model artifact")
    if not set(options) <= set(artifact["model"]["allowed_options"]):
        output["linear_policy"] = {"choice": "abstain", "mode": artifact["mode"],
                                   "reason": "untrained_options"}
    else:
        linear = decide(artifact["model"], {"goal": context["goal"], "facts": context["facts"]}, options)
        output["linear_policy"] = {"choice": linear["choice"],
                                   "probabilities": linear["probabilities"],
                                   "mode": artifact["mode"]}
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("train", "status", "suggest", "compare"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("request", nargs="?")
    args = parser.parse_args()
    dataset_path = args.db.parent / "dataset.json"
    model_path = args.db.parent / "local-linear.json"
    try:
        if args.command == "compare":
            if args.request:
                raise ValueError("compare accepts no request")
            result = compare_source_app(dataset_path)
        elif args.command == "train":
            if args.request:
                raise ValueError("train accepts no request")
            result = train_local(dataset_path, model_path)
        elif args.command == "status":
            if args.request:
                raise ValueError("status accepts no request")
            if not model_path.is_file():
                result = {"offline_model": "absent"}
            else:
                artifact = read_json(model_path, MAX_ARTIFACT)
                result = {key: artifact[key] for key in
                          ("mode", "training_rows", "scope_groups", "validation", "holdout")}
                result["ambiguity"] = artifact.get("ambiguity")
                result["artifact_bytes"] = model_path.stat().st_size
        else:
            if not args.request:
                raise ValueError("symbolic request required")
            result = suggest_all(read_json(Path(args.request), 4096), args.db, model_path)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(f"offline_model=refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
