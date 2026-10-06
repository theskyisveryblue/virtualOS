#!/usr/bin/env python3
"""Adapt reviewed app/site playbooks to the local symbolic decision learner."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from intent_plan import validate_playbook
from offline_model import suggest_all
from offline_policy import SCHEMA, observe, open_db, validate_context
from small_decider import symbol, unique_pairs


def read_json(path: Path, limit: int) -> dict:
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("bounded JSON file required")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    if type(value) is not dict:
        raise ValueError("JSON object required")
    return value


def action_id(step: dict) -> str:
    action = step["action"]
    return symbol(action.get("id", action["verb"] + "." + action["target"]))


def context(playbook: dict, facts: object) -> dict:
    value = {"scope": {key: playbook["scope"][key] for key in ("kind", "id", "version")},
             "goal": playbook["goal"], "facts": facts}
    return validate_context(value)


def options(playbook: dict) -> list[str]:
    values = list(dict.fromkeys(action_id(step) for step in playbook["steps"]))
    if "abstain" in values:
        raise ValueError("reserved action ID")
    return values + ["abstain"]


def suggest_playbook(playbook: dict, state: dict, db_path: Path) -> dict:
    if set(state) not in ({"facts"}, {"facts", "source_app"}):
        raise ValueError("state must contain symbolic facts and optional source app")
    request = {"schema": SCHEMA, "context": context(playbook, state["facts"]),
               "options": options(playbook)}
    if "source_app" in state:
        request["source_app"] = state["source_app"]
    result = suggest_all(request, db_path, db_path.parent / "local-linear.json")
    return {"playbook_id": playbook["playbook_id"], "revision": playbook["revision"],
            "result": result}


def observe_playbook(playbook: dict, event: dict, db_path: Path) -> dict:
    required = {"step", "facts", "outcome", "verifier", "review_id"}
    if set(event) not in (required, required | {"source_app"}):
        raise ValueError("unexpected playbook event fields")
    index = event["step"]
    if type(index) is not int or not 0 <= index < len(playbook["steps"]):
        raise ValueError("invalid playbook step")
    if type(event["outcome"]) is not str or event["outcome"] not in {
            "verified_success", "verified_failure"}:
        raise ValueError("verified outcome required")
    expected = playbook["steps"][index]["verify"]
    verifier = event["verifier"]
    if (type(verifier) is not dict or set(verifier) != {"kind", "state", "independent"}
            or verifier["kind"] != expected["kind"]
            or verifier["state"] != expected["state"]
            or verifier["independent"] is not True):
        raise ValueError("independent verifier does not match playbook")
    mapped_kind = "app_model" if verifier["kind"] == "guest_app" else verifier["kind"]
    if mapped_kind not in {"ax", "ocr", "app_model", "filesystem"}:
        raise ValueError("verifier kind is not available to local learner")
    review = event["review_id"]
    if type(review) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,95}", review):
        raise ValueError("review ID required")
    connection = open_db(db_path)
    try:
        feedback = {"schema": SCHEMA,
                    "context": context(playbook, event["facts"]),
                    "action": action_id(playbook["steps"][index]),
                    "outcome": event["outcome"],
                    "verifier": {"kind": mapped_kind, "state": verifier["state"],
                                 "independent": True},
                    "review_id": review,
                    "rights": {"ownership": "local-use", "training": "local-only",
                               "consent_id": "user.local"}}
        if "source_app" in event:
            feedback["source_app"] = event["source_app"]
        result = observe(connection, feedback)
    finally:
        connection.close()
    return {"playbook_id": playbook["playbook_id"], "revision": playbook["revision"],
            "result": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("suggest", "observe"))
    parser.add_argument("playbook", type=Path)
    parser.add_argument("state_or_event", type=Path)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    try:
        playbook = validate_playbook(read_json(args.playbook, 16_384))
        data = read_json(args.state_or_event, 4096)
        result = (suggest_playbook(playbook, data, args.db) if args.command == "suggest"
                  else observe_playbook(playbook, data, args.db))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(f"playbook_decision=refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
