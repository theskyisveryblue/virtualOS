#!/usr/bin/env python3
"""Read-only Xcode project navigator with independently parsed outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

TASKS = {
    "project-name": "name",
    "list-schemes": "schemes",
    "list-targets": "targets",
    "list-configurations": "configurations",
    "build-settings": "build_settings",
}
MAX_OUTPUT = 131_072


def inventory(repo: Path) -> dict:
    project = repo / "virtualOS.xcodeproj"
    pbx = project / "project.pbxproj"
    if not pbx.is_file() or pbx.stat().st_size > 2_000_000:
        raise ValueError("bounded project file required")
    process = subprocess.run(["/usr/bin/xcodebuild", "-list", "-json", "-project",
                              str(project)], cwd=repo, capture_output=True,
                             timeout=20, check=True)
    if len(process.stdout) > MAX_OUTPUT:
        raise ValueError("Xcode output limit exceeded")
    payload = json.loads(process.stdout)
    if set(payload) != {"project"}:
        raise ValueError("unexpected Xcode output")
    record = payload["project"]
    if set(record) != {"name", "schemes", "targets", "configurations"}:
        raise ValueError("unexpected project fields")
    for key in ("schemes", "targets", "configurations"):
        if (type(record[key]) is not list or len(record[key]) > 100
                or any(type(value) is not str or len(value) > 100 for value in record[key])):
            raise ValueError("unexpected project member")
    build = subprocess.run(["/usr/bin/xcodebuild", "-showBuildSettings", "-json",
                            "-project", str(project), "-scheme", "virtualOS",
                            "-derivedDataPath", "/private/tmp/vos-xcode-derived"],
                           cwd=repo, capture_output=True, timeout=20, check=True)
    if len(build.stdout) > 1_000_000:
        raise ValueError("Xcode build settings output limit exceeded")
    settings_payload = json.loads(build.stdout)
    if (type(settings_payload) is not list or len(settings_payload) != 1
            or settings_payload[0].get("error") is not None
            or settings_payload[0].get("target") != "virtualOS"):
        raise ValueError("Xcode build settings unavailable")
    all_settings = settings_payload[0].get("buildSettings", {})
    if type(all_settings) is not dict:
        raise ValueError("unexpected Xcode build settings")
    selected = {key: all_settings.get(key) for key in
                ("PRODUCT_NAME", "SWIFT_VERSION", "MACOSX_DEPLOYMENT_TARGET",
                 "PRODUCT_BUNDLE_IDENTIFIER", "SUPPORTED_PLATFORMS")}
    selected["SDKROOT"] = Path(all_settings.get("SDKROOT", "")).name
    if any(type(value) is not str or len(value) > 150 for value in selected.values()):
        raise ValueError("unexpected selected build setting")
    return {"schema": "virtualos.xcode-inventory/v1", "xcode_version":
            subprocess.run(["/usr/bin/xcodebuild", "-version"], capture_output=True,
                           text=True, timeout=10, check=True).stdout.strip().replace("\n", " / "),
            "project_sha256": hashlib.sha256(pbx.read_bytes()).hexdigest(),
            "project": {**record, "build_settings": selected},
            "source": "observed_xcodebuild_json",
            "training_rights": "local-only"}


def task_result(record: dict, task: str) -> dict:
    key = TASKS[task]
    value = record["project"][key]
    return {"schema": "virtualos.xcode-task-result/v1", "task": task,
            "route": "local_xcode_cli", "verified": True, "value": value,
            "project_sha256": record["project_sha256"]}


def route(text: str) -> str | None:
    if len(text) > 500:
        raise ValueError("task request too long")
    request = text.lower()
    patterns = {
        "project-name": r"\bproject name\b",
        "list-schemes": r"\bschemes?\b",
        "list-targets": r"\btargets?\b",
        "list-configurations": r"\bconfigurations?\b|\bdebug and release\b",
        "build-settings": r"\bbuild settings?\b|\bbundle (?:id|identifier)\b|\bsdk\b|\bswift version\b|\bdeployment target\b|\bsupported platforms?\b",
    }
    matches = [task for task, pattern in patterns.items() if re.search(pattern, request)]
    if "build-settings" in matches and "list-targets" in matches and "deployment target" in request:
        matches.remove("list-targets")
    return matches[0] if len(matches) == 1 else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("inventory", "collect", "task", "route"))
    parser.add_argument("task", nargs="?")
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    if args.command in {"task", "route"} and not args.task:
        parser.error("task or request required")
    if args.command in {"inventory", "collect"} and args.task:
        parser.error("unexpected task")
    if args.command == "task" and args.task not in TASKS:
        parser.error("unsupported task ID")
    if args.command == "route":
        task = route(args.task)
        if task is None:
            print(json.dumps({"xcode_route": "abstain", "reason": "unknown_or_ambiguous_goal"}))
            return
    record = inventory(args.repo.resolve())
    if args.command == "collect":
        destination = args.repo / "tools" / "data" / "xcode-inventory.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=destination.parent, delete=False,
                                         encoding="utf-8", prefix=".xcode-inventory-") as handle:
            json.dump(record, handle, sort_keys=True, indent=2)
            handle.write("\n")
            temp = Path(handle.name)
        os.replace(temp, destination)
        print(json.dumps({"xcode_inventory": "collected", "tasks": list(TASKS),
                          "project_sha256": record["project_sha256"]}, sort_keys=True))
    elif args.command == "task":
        print(json.dumps(task_result(record, args.task), sort_keys=True))
    elif args.command == "route":
        print(json.dumps(task_result(record, task), sort_keys=True))
    else:
        print(json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    main()
