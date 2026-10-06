#!/usr/bin/env python3
"""Metadata-only audit of a bounded UiPad macOS accessibility sample."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

SCHEMA = "virtualos.macos-ax-audit/v1"
REVISION = "2aa68a2a57c49c66e29d745fdd64547564f4a758"


def questions(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size > 200_000:
        raise ValueError("bounded question table required")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows or len(rows) > 1200 or set(rows[0]) != {
            "App bundle", "App name", "Screen id", "Question", "Answer", "Answer Type"}:
        raise ValueError("unexpected question table")
    return rows


def tree_counts(path: Path) -> dict:
    if path.stat().st_size > 3_000_000:
        raise ValueError("AX tree too large")
    root = json.loads(path.read_text(encoding="utf-8"))
    stack = [(root, 0)]
    roles = Counter()
    deepest = 0
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict) or depth > 80 or len(roles) > 200:
            raise ValueError("invalid AX tree")
        roles[str(node.get("role", "unknown"))] += 1
        deepest = max(deepest, depth)
        children = node.get("children", [])
        if not isinstance(children, list) or len(children) > 5000:
            raise ValueError("invalid AX children")
        stack.extend((child, depth + 1) for child in children)
        if sum(roles.values()) > 20_000:
            raise ValueError("AX tree node limit")
    return {"nodes": sum(roles.values()), "max_depth": deepest, "roles": roles}


def audit(root: Path) -> dict:
    train = questions(root / "ui_questions_train.tsv")
    test = questions(root / "ui_questions_test.tsv")
    screens = {split: {(row["App bundle"], row["Screen id"]) for row in rows}
               for split, rows in (("train", train), ("test", test))}
    apps = {split: {item[0] for item in screen_set}
            for split, screen_set in screens.items()}
    paths = sorted((root / "app_data" / "VSCode").glob("*/*.json"))
    if len(paths) != 7:
        raise ValueError("expected seven VSCode AX trees")
    role_counts = Counter()
    nodes = []
    depths = []
    for path in paths:
        count = tree_counts(path)
        role_counts.update(count["roles"])
        nodes.append(count["nodes"])
        depths.append(count["max_depth"])
    return {"schema": SCHEMA, "source": "macpaw-research/UiPad",
            "revision": REVISION, "license": "mit", "images_included": False,
            "ui_text_included": False, "train_questions": len(train),
            "test_questions": len(test), "train_screens": len(screens["train"]),
            "test_screens": len(screens["test"]),
            "shared_screens": len(screens["train"] & screens["test"]),
            "shared_apps": len(apps["train"] & apps["test"]),
            "xcode_questions": sum("xcode" in row["App name"].lower()
                                   for row in train + test),
            "vscode_ax_trees": len(paths), "vscode_ax_nodes_min": min(nodes),
            "vscode_ax_nodes_max": max(nodes), "vscode_ax_depth_max": max(depths),
            "vscode_roles": dict(sorted(role_counts.items())),
            "question_table_sha256": {
                split: hashlib.sha256((root / f"ui_questions_{split}.tsv").read_bytes()).hexdigest()
                for split in ("train", "test")}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--write", type=Path)
    args = parser.parse_args()
    result = audit(args.root)
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
