#!/usr/bin/env python3
"""Validate compact, content-free guest computer-use intent plans.

This module only compiles policy. It never observes or acts on a computer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


SCHEMA = "virtualos.intent-plan/v1"
TOKEN = re.compile(r"^[a-z][a-z0-9_-]*(?:[.][a-z0-9_-]+)*$")
APP = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:[.][A-Za-z0-9_-]+)+$")
MAX_SOURCE_BYTES = 4096
MAX_PLAYBOOK_BYTES = 16384
ALLOWED_VERBS = {"click", "double_click", "key", "scroll", "focus", "open"}
ALLOWED_OBSERVATIONS = {"ax", "ocr", "screenshot"}
ALLOWED_VERIFIERS = {"ax", "ocr"}


class PlanError(ValueError):
    pass


def _fields(value, fields: set[str], label: str):
    if not isinstance(value, dict) or set(value) != fields:
        raise PlanError(f"{label} has missing or unexpected fields")


def _symbol(value, label: str) -> str:
    if not isinstance(value, str) or len(value) > 96 or not TOKEN.fullmatch(value):
        raise PlanError(f"{label} must be a bounded symbolic identifier")
    return value


def _one_of(value: object, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise PlanError(f"unsupported {label}")
    return value


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def validate_playbook(value: object) -> dict:
    """Accept only a content-free, private playbook manifest; never execute it."""
    _fields(value, {"schema", "playbook_id", "revision", "scope", "goal", "steps",
                    "rights", "privacy", "split", "source"}, "playbook")
    if value["schema"] != "virtualos.playbook/v1":
        raise PlanError("unsupported playbook schema")
    _symbol(value["playbook_id"], "playbook_id")
    _symbol(value["goal"], "goal")
    if type(value["revision"]) is not int or not 1 <= value["revision"] <= 9999:
        raise PlanError("revision must be an integer from 1 to 9999")
    scope = value["scope"]
    _fields(scope, {"kind", "id", "version", "profile_id"}, "scope")
    _one_of(scope["kind"], {"app", "site"}, "scope kind")
    _symbol(scope["profile_id"], "profile_id")
    if scope["kind"] == "app":
        _identifier(scope["id"], 0, "app", APP)
    else:
        origin = scope["id"]
        if not isinstance(origin, str) or len(origin) > 253:
            raise PlanError("site ID must be a bounded HTTPS origin")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or not re.fullmatch(r"(?:[a-z0-9-]+[.])+[a-z]{2,63}", parsed.hostname)
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or parsed.geturl() != origin or parsed.netloc != parsed.hostname):
            raise PlanError("site ID must be an HTTPS origin without credentials, path, or query")
    version = scope["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise PlanError("scope version must be a bounded release identifier")
    steps = value["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        raise PlanError("steps must contain 1 to 8 entries")
    for index, step in enumerate(steps):
        _fields(step, {"action", "expect", "verify"}, f"steps[{index}]")
        action_fields = set(step["action"]) if type(step["action"]) is dict else set()
        if action_fields not in ({"verb", "target"}, {"verb", "target", "id"}):
            raise PlanError(f"unexpected steps[{index}].action fields")
        _one_of(step["action"]["verb"], ALLOWED_VERBS, "playbook action")
        _symbol(step["action"]["target"], "action target")
        if "id" in step["action"]:
            if len(_symbol(step["action"]["id"], "action id")) > 64:
                raise PlanError("action id exceeds local chooser limit")
        _symbol(step["expect"], "expected state")
        _fields(step["verify"], {"kind", "state"}, f"steps[{index}].verify")
        _one_of(step["verify"]["kind"], {"ax", "ocr", "guest_app", "filesystem", "app_model"}, "verifier kind")
        if _symbol(step["verify"]["state"], "verifier state") != step["expect"]:
            raise PlanError("verifier state differs from expected state")
    rights = value["rights"]
    _fields(rights, {"ownership", "training", "consent_id"}, "rights")
    _one_of(rights["ownership"], {"owned", "licensed", "evaluation-only", "unknown"}, "ownership right")
    _one_of(rights["training"], {"allowed", "denied", "unknown"}, "training right")
    if rights["consent_id"] is not None:
        _symbol(rights["consent_id"], "consent_id")
    if rights["training"] == "allowed" and (rights["ownership"] not in {"owned", "licensed"}
                                               or rights["consent_id"] is None):
        raise PlanError("training requires owned/licensed rights and consent provenance")
    privacy = value["privacy"]
    _fields(privacy, {"mode", "cloud"}, "privacy")
    if privacy != {"mode": "metadata-only", "cloud": "private-r2"}:
        raise PlanError("playbook privacy must be metadata-only in private R2")
    split = value["split"]
    _fields(split, {"role", "group_id", "policy", "review_id"}, "split")
    _one_of(split["role"], {"unassigned", "train", "validation", "holdout"}, "split role")
    _symbol(split["group_id"], "split group_id")
    if split["review_id"] is not None:
        _symbol(split["review_id"], "split review_id")
    if (split["role"] == "unassigned") != (split["review_id"] is None):
        raise PlanError("split assignment requires review provenance")
    if split["policy"] != "goal-and-state-v1":
        raise PlanError("unsupported split policy")
    if split["role"] != "unassigned" and rights["training"] != "allowed":
        raise PlanError("assigned split requires training rights")
    source = value["source"]
    _fields(source, {"kind", "sha256", "evidence"}, "source")
    _one_of(source["kind"], {"xcanvas-profile", "agent-projection", "virtualos-episode", "declared-fixture"}, "source kind")
    if not isinstance(source["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
        raise PlanError("source requires a SHA-256 provenance digest")
    _one_of(source["evidence"], {"observed", "declared"}, "source evidence")
    if split["role"] != "unassigned" and source["evidence"] != "observed":
        raise PlanError("assigned split requires observed evidence")
    return value


def playbook_object_key(value: dict) -> str:
    scope = value["scope"]
    scope_key = hashlib.sha256((scope["kind"] + "\0" + scope["id"] + "\0" + scope["version"]).encode()).hexdigest()[:20]
    id_key = hashlib.sha256(value["playbook_id"].encode()).hexdigest()[:20]
    content_key = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"playbooks/v1/{scope['kind']}/{scope_key}/{id_key}/r{value['revision']}-{content_key}.json"


def _identifier(value: str, line: int, label: str, pattern=TOKEN) -> str:
    if not isinstance(value, str) or len(value) > 96 or not pattern.fullmatch(value):
        raise PlanError(f"line {line}: invalid {label} identifier")
    return value


def parse_plan(source: str) -> dict:
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise PlanError("plan exceeds 4096 bytes")
    plan: dict = {"schema": SCHEMA, "observations": ["ax"], "fallback": "none"}
    seen: set[str] = set()
    actions: list[dict] = []
    for number, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        keyword, args = parts[0], parts[1:]
        if keyword == "allow":
            if len(args) != 2:
                raise PlanError(f"line {number}: allow requires verb and symbolic target")
            verb = _identifier(args[0], number, "verb")
            target = _identifier(args[1], number, "target")
            if verb not in ALLOWED_VERBS:
                raise PlanError(f"line {number}: unsupported action")
            if len(actions) >= 12 or any(item["verb"] == verb and item["target"] == target for item in actions):
                raise PlanError(f"line {number}: duplicate action or action limit exceeded")
            actions.append({"verb": verb, "target": target})
            continue
        if keyword in seen:
            raise PlanError(f"line {number}: duplicate {keyword}")
        seen.add(keyword)
        if keyword in {"app", "goal", "expect"}:
            if len(args) != 1:
                raise PlanError(f"line {number}: {keyword} requires one identifier")
            plan[keyword] = _identifier(args[0], number, keyword, APP if keyword == "app" else TOKEN)
        elif keyword == "observe":
            if not 1 <= len(args) <= 3 or len(args) != len(set(args)) or any(a not in ALLOWED_OBSERVATIONS for a in args):
                raise PlanError(f"line {number}: invalid observation channels")
            plan["observations"] = args
        elif keyword == "verify":
            if len(args) != 2 or args[0] not in ALLOWED_VERIFIERS:
                raise PlanError(f"line {number}: verify requires ax|ocr and symbolic state")
            plan["verifier"] = {"channel": args[0], "state": _identifier(args[1], number, "state")}
        elif keyword == "privacy":
            if args != ["local", "metadata-only"]:
                raise PlanError(f"line {number}: privacy must be local metadata-only")
            plan["privacy"] = {"scope": "local", "retention": "metadata-only"}
        elif keyword == "fallback":
            if args not in (["none"], ["screenshot-on-ambiguity"]):
                raise PlanError(f"line {number}: invalid fallback")
            plan["fallback"] = args[0]
        else:
            raise PlanError(f"line {number}: unknown directive")
    required = {"app", "goal", "expect", "verifier", "privacy"}
    missing = required - plan.keys()
    if missing:
        raise PlanError("missing directives: " + ", ".join(sorted(missing)))
    if plan["verifier"]["state"] != plan["expect"]:
        raise PlanError("verifier state must equal expected state")
    if plan["verifier"]["channel"] not in plan["observations"]:
        raise PlanError("verifier channel must be observable")
    if "screenshot" in plan["observations"] and plan["fallback"] != "screenshot-on-ambiguity":
        raise PlanError("screenshot observation requires explicit fallback")
    if plan["fallback"] == "screenshot-on-ambiguity" and "screenshot" not in plan["observations"]:
        raise PlanError("screenshot fallback requires screenshot observation")
    plan["allowed_actions"] = actions
    plan["execution"] = "disabled-until-bound-to-reviewed-profile"
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "compile", "playbook-validate", "playbook-export"])
    parser.add_argument("plan", help="plan file, or - for stdin")
    args = parser.parse_args()
    try:
        limit = MAX_PLAYBOOK_BYTES if args.command.startswith("playbook-") else MAX_SOURCE_BYTES
        if args.plan == "-":
            source = sys.stdin.read(limit + 1)
        else:
            path = Path(args.plan)
            if path.stat().st_size > limit:
                raise PlanError("input exceeds size limit")
            source = path.read_text(encoding="utf-8")
        if args.command.startswith("playbook-"):
            if len(source.encode("utf-8")) > limit:
                raise PlanError("input exceeds size limit")
            playbook = validate_playbook(json.loads(source, object_pairs_hook=_unique_pairs))
        else:
            plan = parse_plan(source)
    except (PlanError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"intent-plan: {error}", file=sys.stderr)
        return 2
    if args.command == "playbook-export":
        print(json.dumps({"object_key": playbook_object_key(playbook), "playbook": playbook}, sort_keys=True, separators=(",", ":")))
    elif args.command == "playbook-validate":
        print("playbook=valid")
    elif args.command == "compile":
        print(json.dumps(plan, sort_keys=True, separators=(",", ":")))
    else:
        print("intent_plan=valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
