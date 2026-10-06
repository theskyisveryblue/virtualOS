#!/usr/bin/env python3
"""Computer-use adapters for the virtualOS guest. No host screen or input APIs."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
from http.client import IncompleteRead
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    width: int
    height: int
    capture_width: int
    capture_height: int

    @property
    def likely_blank(self) -> bool:
        return len(self.jpeg) < max(8000, min(50000, int(self.width * self.height * 0.02)))

    @property
    def data_url(self) -> str:
        return "data:image/jpeg;base64," + base64.b64encode(self.jpeg).decode("ascii")


class GuestVM:
    def __init__(self, url: str | None = None):
        self.url = (url or os.getenv("VOS_VM_URL", "http://127.0.0.1:8845")).rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("VOS_VM_URL must be a local HTTP endpoint; use an SSH tunnel for a remote Mac")
        self.last_point = (0, 0)

    def _request(self, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        for attempt in range(2):
            request = Request(self.url + path, data=body, headers={"Content-Type": "application/json"} if body else {})
            try:
                with urlopen(request, timeout=30 if path == "/vm/exec" else 15) as response:
                    raw = response.read()
                    if path == "/vm/screen":
                        return raw, response.headers
                    result = json.loads(raw)
                    if not result.get("ok", False):
                        raise RuntimeError(result.get("error", "guest command failed"))
                    return result
            except (OSError, IncompleteRead):
                # Retrying input actions can send them twice. Only reads and
                # pointer motion are safe to repeat after a socket reset.
                if attempt or path not in {"/vm/health", "/vm/screen", "/vm/mouse/move"}:
                    raise
                time.sleep(0.25)

    def health(self) -> dict:
        result = self._request("/vm/health")
        if not result.get("ready"):
            raise RuntimeError("guest VM is not ready")
        return result

    def frame(self, allow_blank: bool = False) -> Frame:
        self.health()
        frame = self._capture_frame()
        if frame.likely_blank and not allow_blank:
            # A blank capture does not prove where the guest focus is. Retry
            # reads without sending input into an unknown screen.
            for delay in (0.5, 1.0, 2.0):
                time.sleep(delay)
                frame = self._capture_frame()
                if not frame.likely_blank:
                    break
            if frame.likely_blank:
                raise RuntimeError("guest screenshot stayed blank after read-only retries")
        return frame

    def _capture_frame(self) -> Frame:
        jpeg, headers = self._request("/vm/screen")
        width = int(headers["X-Image-Width"])
        height = int(headers["X-Image-Height"])
        capture_width = int(headers["X-Capture-Width"])
        capture_height = int(headers["X-Capture-Height"])
        if not jpeg.startswith(b"\xff\xd8") or width < 1 or height < 1 or capture_width < 1 or capture_height < 1:
            raise RuntimeError("guest screenshot is invalid")
        return Frame(jpeg, width, height, capture_width, capture_height)

    def _point(self, x: int | float, y: int | float, frame: Frame) -> tuple[int, int]:
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y)):
            raise ValueError("coordinates must be finite numbers")
        if not (0 <= x < frame.width and 0 <= y < frame.height):
            raise ValueError(f"coordinates outside guest screenshot {frame.width}x{frame.height}")
        # The VM view uses a bottom-left origin. Model screenshots use top-left.
        return round(x * frame.capture_width / frame.width), round((frame.height - y) * frame.capture_height / frame.height)

    def click(self, x: int | float, y: int | float, button: str = "left", count: int = 1, frame: Frame | None = None):
        if button not in {"left", "right"} or count not in {1, 2, 3}:
            raise ValueError("unsupported mouse button or click count")
        frame = frame or self.frame()
        px, py = self._point(x, y, frame)
        self._request("/vm/click", {"x": px, "y": py, "button": button, "count": count})
        self.last_point = (x, y)

    def move(self, x: int | float, y: int | float, frame: Frame | None = None):
        frame = frame or self.frame()
        px, py = self._point(x, y, frame)
        self._request("/vm/mouse/move", {"x": px, "y": py})
        self.last_point = (x, y)

    def drag(self, x: int | float, y: int | float, from_x=None, from_y=None, frame: Frame | None = None):
        frame = frame or self.frame()
        start_x = self.last_point[0] if from_x is None else from_x
        start_y = self.last_point[1] if from_y is None else from_y
        fx, fy = self._point(start_x, start_y, frame)
        tx, ty = self._point(x, y, frame)
        self._request("/vm/drag", {"fromX": fx, "fromY": fy, "toX": tx, "toY": ty, "steps": 20})
        self.last_point = (x, y)

    def scroll(self, pixels: int, x=None, y=None, horizontal: int = 0, frame: Frame | None = None):
        frame = frame or self.frame()
        x = frame.width / 2 if x is None else x
        y = frame.height / 2 if y is None else y
        px, py = self._point(x, y, frame)
        self._request("/vm/scroll", {"x": px, "y": py, "dy": round(pixels / 100), "dx": round(horizontal / 100)})

    def type(self, value: str):
        self._request("/vm/paste", {"text": value})

    def key(self, keys: list[str] | str):
        if isinstance(keys, str):
            keys = [keys]
        if not keys:
            raise ValueError("empty key list")
        aliases = {"ENTER": "return", "RETURN": "return", "ESC": "escape", "BACKSPACE": "delete", "CMD": "command", "META": "command", "SUPER": "command", "WIN": "command", "CONTROLORMETA": "command", "CTRL": "control", "ALT": "option", "ARROWUP": "up", "ARROWDOWN": "down", "ARROWLEFT": "left", "ARROWRIGHT": "right"}
        normalized = [aliases.get(str(k).upper(), str(k).lower()) for k in keys]
        modifiers = [k for k in normalized if k in {"command", "control", "option", "shift"}]
        regular = [k for k in normalized if k not in modifiers]
        if len(regular) != 1:
            raise ValueError("one key plus optional modifiers is required")
        path = "/vm/shortcut" if modifiers else "/vm/key"
        payload = {"key": regular[0], "modifiers": modifiers} if modifiers else {"key": regular[0]}
        self._request(path, payload)


def foundry_action(vm: GuestVM, action: dict, frame: Frame):
    kind = action.get("type")
    if kind == "screenshot":
        return
    if kind == "click":
        vm.click(action["x"], action["y"], action.get("button", "left"), frame=frame)
    elif kind == "double_click":
        vm.click(action["x"], action["y"], count=2, frame=frame)
    elif kind in {"move", "mouse_move"}:
        vm.move(action["x"], action["y"], frame=frame)
    elif kind == "drag":
        path = action["path"]
        if len(path) < 2:
            raise ValueError("drag needs at least two points")
        vm.drag(path[-1]["x"], path[-1]["y"], path[0]["x"], path[0]["y"], frame=frame)
    elif kind == "scroll":
        vm.scroll(-int(action.get("scroll_y", 0)), action.get("x"), action.get("y"), int(action.get("scroll_x", 0)), frame=frame)
    elif kind == "type":
        vm.type(action["text"])
    elif kind in {"key", "keypress"}:
        vm.key(action["keys"])
    elif kind == "wait":
        time.sleep(min(max(float(action.get("duration", 1)), 0), 10))
    else:
        raise ValueError(f"unsupported Foundry action: {kind}")


def _azure_token() -> str:
    token = os.getenv("FOUNDRY_TOKEN")
    if token:
        return token
    result = subprocess.run(["az", "account", "get-access-token", "--scope", "https://ai.azure.com/.default", "--query", "accessToken", "-o", "tsv"], capture_output=True, text=True, timeout=20, check=True)
    return result.stdout.strip()


def _foundry_post(endpoint: str, path: str, payload: dict, token: str) -> dict:
    request = Request(endpoint.rstrip("/") + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urlopen(request, timeout=120) as response:
        return json.load(response)


def run_foundry(vm: GuestVM, task: str, max_steps: int):
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    token = _azure_token()
    frame = vm.frame()
    agent_name = "virtualos-computer-use-" + uuid.uuid4().hex[:8]
    _foundry_post(endpoint, "/agents?api-version=v1", {"name": agent_name, "definition": {"kind": "prompt", "model": "computer-use-preview", "instructions": "Operate only the provided macOS guest. Use screenshots to verify actions. Stop and ask before irreversible actions.", "tools": [{"type": "computer_use_preview", "environment": "mac", "display_width": frame.width, "display_height": frame.height}]}}, token)
    try:
        response = _foundry_post(endpoint, "/openai/v1/responses", {"agent_reference": {"type": "agent_reference", "name": agent_name}, "input": [{"role": "user", "content": [{"type": "input_text", "text": task}, {"type": "input_image", "image_url": frame.data_url, "detail": "high"}]}]}, token)
        for step in range(max_steps):
            calls = [item for item in response.get("output", []) if item.get("type") == "computer_call"]
            if not calls:
                print(json.dumps({"status": "complete", "output": response.get("output", [])}))
                return
            call = calls[0]
            checks = call.get("pending_safety_checks") or []
            if checks:
                print(json.dumps({"status": "approval_required", "checks": checks, "action": call.get("action")}))
                if not sys.stdin.isatty() or input("Approve this Foundry action? Type yes: ").strip().lower() != "yes":
                    return
            foundry_action(vm, call["action"], frame)
            frame = vm.frame()
            response = _foundry_post(endpoint, "/openai/v1/responses", {"agent_reference": {"type": "agent_reference", "name": agent_name}, "previous_response_id": response["id"], "input": [{"type": "computer_call_output", "call_id": call["call_id"], "acknowledged_safety_checks": checks, "output": {"type": "computer_screenshot", "image_url": frame.data_url}}]}, token)
            print(json.dumps({"step": step + 1, "action": call["action"].get("type")}))
        print(json.dumps({"status": "step_limit", "steps": max_steps}))
    finally:
        try:
            request = Request(endpoint.rstrip("/") + f"/agents/{agent_name}?api-version=v1", method="DELETE", headers={"Authorization": "Bearer " + token})
            urlopen(request, timeout=20).close()
        except Exception as exc:
            print(f"Foundry agent cleanup failed: {exc}", file=sys.stderr)


def run_bedrock(vm: GuestVM, task: str, max_steps: int):
    """Use Nova 2 Lite vision as a screenshot-to-one-action planner."""
    import boto3  # Optional dependency; AWS credentials come from the normal SDK chain.

    client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
    model = os.getenv("NOVA_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
    history: list[dict] = []
    for step in range(max_steps):
        frame = vm.frame()
        prompt = (
            f"Task: {task}\nGuest screenshot size: {frame.width}x{frame.height}. "
            "Return exactly one JSON object with one action: "
            '{"type":"click","x":int,"y":int}, {"type":"type","text":string}, '
            '{"type":"key","keys":[string]}, {"type":"scroll","scroll_y":int}, '
            '{"type":"screenshot"}, or {"type":"done","answer":string}. '
            "Coordinates start at the top left. Positive scroll_y scrolls down. "
            "Do not request irreversible actions without user approval."
        )
        messages = history[-6:] + [{"role": "user", "content": [{"text": prompt}, {"image": {"format": "jpeg", "source": {"bytes": frame.jpeg}}}]}]
        response = client.converse(modelId=model, messages=messages, inferenceConfig={"maxTokens": 300, "temperature": 0})
        reply = "".join(block.get("text", "") for block in response["output"]["message"]["content"])
        try:
            action = json.loads(reply.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError as exc:
            raise RuntimeError("Nova returned an invalid action") from exc
        if action.get("type") == "done":
            print(json.dumps({"status": "complete", "answer": action.get("answer", "")}))
            return
        foundry_action(vm, action, frame)
        history = messages + [{"role": "assistant", "content": [{"text": reply}]}]
        print(json.dumps({"step": step + 1, "action": action["type"]}))
    print(json.dumps({"status": "step_limit", "steps": max_steps}))


def _ollama_action(frame: Frame, task: str, model: str, retry: bool = False) -> dict:
    endpoint = os.getenv("VOS_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("VOS_OLLAMA_URL must be a local endpoint")
    keep_alive = os.getenv("VOS_OLLAMA_KEEP_ALIVE", "60s")
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,3}[smh])", keep_alive):
        raise ValueError("VOS_OLLAMA_KEEP_ALIVE must be 0 or a bounded s/m/h duration")
    prompt = (
        f"Task: {task}\n"
        "Look at this macOS screenshot. Find the visible UI target for the task. "
        'When a click is needed, return one JSON object with point_2d as [x,y] and label as the target name. '
        "point_2d uses a 0-1000 scale for both axes, from the top left. "
        'If the target is not visible, return {"type":"abstain"}. '
        'Only return {"type":"done"} when the requested task is already visibly complete. '
        'Never guess a point.'
    )
    if retry:
        prompt += (' Return only one JSON object with point_2d and label for a visible icon. '
                   'If uncertain, return {"type":"abstain"}. Never return {}.')
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(frame.jpeg).decode("ascii")]}],
        "format": "json",
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        "options": {"temperature": 0, "num_predict": 256},
    }).encode("utf-8")
    request = Request(endpoint + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=120) as response:
        payload = json.load(response)
    raw = payload.get("message", {}).get("content", "")
    if not raw:
        return (_ollama_action(frame, task, model, retry=True) if not retry else
                {"type": "abstain", "reason": "empty_model_response"})
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    candidate = fenced.group(1).strip() if fenced else raw.strip()
    if candidate and candidate[0] not in "[{":
        starts = [index for index in (candidate.find("{"), candidate.find("[")) if index >= 0]
        candidate = candidate[min(starts):] if starts else candidate
    try:
        action, _ = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"local model returned invalid JSON: chars={len(candidate)} first_codepoint={ord(candidate[0]) if candidate else 0}") from exc
    if isinstance(action, list) and len(action) == 1:
        action = action[0]
    if not isinstance(action, dict) or not action:
        return (_ollama_action(frame, task, model, retry=True) if not retry else
                {"type": "abstain", "reason": "empty_model_action"})
    if isinstance(action, dict) and "point_2d" in action:
        point = action["point_2d"]
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1000 for v in point):
            raise ValueError("local model returned invalid normalized coordinates")
        action = {"type": "click", "x": round(point[0] * (frame.width - 1) / 1000), "y": round(point[1] * (frame.height - 1) / 1000)}
    if isinstance(action, dict) and "type" not in action:
        if "x" in action and "y" in action:
            action["type"] = "click"
        elif "scroll_y" in action:
            action["type"] = "scroll"
        elif "keys" in action:
            action["type"] = "key"
        elif "text" in action:
            action["type"] = "type"
    if not isinstance(action, dict) or action.get("type") not in {
        "click", "double_click", "scroll", "key", "type", "screenshot", "done", "abstain"
    }:
        keys = sorted(action) if isinstance(action, dict) else []
        raise ValueError(f"local model returned an unsupported action shape: keys={keys}")
    return action


def _record_local_step(root: Path, frame: Frame, event: dict, after: Frame | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    frames = root / "frames"
    frames.mkdir(exist_ok=True, mode=0o700)
    # A proposal with no guest action has no outcome image to compare. Keep
    # its symbolic trace and frame hash, but do not retain the private pixels.
    retain_frames = event.get("executed") is True and after is not None
    incoming_bytes = len(frame.jpeg) + len(after.jpeg) if retain_frames else 0
    existing_bytes = sum(path.stat().st_size for path in frames.glob("*.jpg"))
    if existing_bytes + incoming_bytes > 64_000_000:
        raise RuntimeError("computer-use frame limit reached; review and export the existing traces")
    journal = root / "steps.jsonl"
    if journal.exists() and journal.stat().st_size > 8_000_000:
        raise RuntimeError("computer-use trace limit reached; review and export the existing traces")
    if retain_frames:
        frame_path = frames / (event["id"] + ".jpg")
        frame_path.write_bytes(frame.jpeg)
        frame_path.chmod(0o600)
        event["frame"] = str(frame_path.relative_to(root))
        after_path = frames / (event["id"] + "-after.jpg")
        after_path.write_bytes(after.jpeg)
        after_path.chmod(0o600)
        event["after_frame"] = str(after_path.relative_to(root))
        event["after_sha256"] = hashlib.sha256(after.jpeg).hexdigest()
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    journal.chmod(0o600)


def audit_local_steps(root: Path) -> dict:
    journal = root / "steps.jsonl"
    if not journal.is_file():
        raise FileNotFoundError(f"computer-use trace is missing: {journal}")
    reviews = {}
    review_file = root / "reviews.jsonl"
    if review_file.is_file():
        with review_file.open(encoding="utf-8") as handle:
            for line in handle:
                review = json.loads(line)
                reviews[review["id"]] = review
    counts = {"verified_success": 0, "verified_failure": 0, "unverified": 0, "not_executed": 0}
    by_target: dict[str, dict[str, int]] = {}
    review_candidates = []
    visually_accepted = []
    total = 0
    with journal.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid trace JSON at line {line_number}") from exc
            total += 1
            if not event.get("executed"):
                result = "not_executed"
            elif event.get("verified") is True:
                result = "verified_success"
            elif event.get("verified") is False:
                result = "verified_failure"
            else:
                result = "unverified"
            counts[result] += 1
            target = event.get("target_app") or "other"
            target_counts = by_target.setdefault(target, {key: 0 for key in counts})
            target_counts[result] += 1
            if (result == "verified_success" and event.get("action") == "click"
                    and event.get("frame") and event.get("after_frame")
                    and (root / event["frame"]).is_file() and (root / event["after_frame"]).is_file()):
                review_candidates.append(event["id"])
                review = reviews.get(event["id"])
                if (review and review.get("decision") == "accept"
                        and review.get("frame_sha256") == event.get("frame_sha256")
                        and review.get("after_sha256") == event.get("after_sha256")
                        and hashlib.sha256((root / event["frame"]).read_bytes()).hexdigest() == event["frame_sha256"]
                        and hashlib.sha256((root / event["after_frame"]).read_bytes()).hexdigest() == event["after_sha256"]):
                    visually_accepted.append(event["id"])
    return {"total": total, "counts": counts, "by_target": by_target,
            "review_candidate_ids": review_candidates,
            "visually_accepted_ids": visually_accepted,
            "note": "OCR verified only the active app name; inspect both guest frames before approving any training example."}


def review_local_step(root: Path, trace_id: str, decision: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", trace_id):
        raise ValueError("review ID must be a 32-character trace ID")
    if decision not in {"accept", "reject"}:
        raise ValueError("review decision must be accept or reject")
    journal = root / "steps.jsonl"
    with journal.open(encoding="utf-8") as handle:
        matches = [event for event in map(json.loads, handle) if event.get("id") == trace_id]
    if len(matches) != 1:
        raise ValueError("review ID must match exactly one trace event")
    event = matches[0]
    if decision == "accept" and not (
        event.get("executed") is True and event.get("verified") is True
        and event.get("action") == "click" and event.get("target_app")
        and event.get("frame") and event.get("after_frame")
        and (root / event["frame"]).is_file()
        and (root / event["after_frame"]).is_file()
    ):
        raise ValueError("only a verified click with both frames can be accepted")
    if decision == "accept" and (
        hashlib.sha256((root / event["frame"]).read_bytes()).hexdigest() != event["frame_sha256"]
        or hashlib.sha256((root / event["after_frame"]).read_bytes()).hexdigest() != event["after_sha256"]
    ):
        raise ValueError("trace frames do not match their recorded hashes")
    review = {"id": trace_id, "decision": decision, "reviewed_at": time.time(),
              "frame_sha256": event.get("frame_sha256"),
              "after_sha256": event.get("after_sha256")}
    review_file = root / "reviews.jsonl"
    if review_file.is_file():
        if review_file.stat().st_size > 1_000_000:
            raise ValueError("review journal exceeds the size limit")
        with review_file.open(encoding="utf-8") as handle:
            if any(json.loads(line).get("id") == trace_id for line in handle if line.strip()):
                raise ValueError("trace already has a review decision")
    with review_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(review, separators=(",", ":")) + "\n")
    review_file.chmod(0o600)
    return review


def _dock_target(task: str) -> str | None:
    match = re.search(r"\b(Finder|Terminal|Safari|Google Chrome|Chrome)(?:\s+[a-z]+){0,2} icon in the guest Dock\b", task, re.I)
    if not match:
        return None
    name = match.group(1).lower()
    return "Google Chrome" if name == "chrome" else name.title()


def _finder_sidebar_target(task: str) -> str | None:
    match = re.fullmatch(r"Click (Applications|Downloads|Documents|Desktop|Recents) in the Finder sidebar", task.strip(), re.I)
    return match.group(1).title() if match else None


_DOCK_SCOPE = {"Finder": "com.apple.finder", "Terminal": "com.apple.Terminal",
               "Safari": "com.apple.Safari", "Google Chrome": "com.google.Chrome"}


def _offline_dock_context(target: str) -> dict:
    return {"scope": {"kind": "app", "id": _DOCK_SCOPE[target], "version": "dock.v1"},
            "goal": "app.frontmost", "facts": ["app.not_frontmost"]}


def _offline_sidebar_context(target_view: str) -> dict:
    return {"scope": {"kind": "app", "id": "com.apple.finder", "version": "sidebar.v1"},
            "goal": f"finder.sidebar.{target_view.lower()}.visible",
            "facts": ["finder.active", "view.not_visible"]}


def _offline_policy_connection():
    from offline_policy import open_db
    path = Path(os.getenv("VOS_OFFLINE_POLICY_DB", str(Path.home() /
                "Library/Application Support/virtualOS/offline-policy/policy.sqlite3")))
    return open_db(path)


def _offline_suggestion(context: dict, action: str) -> dict:
    from offline_policy import SCHEMA, suggest
    connection = _offline_policy_connection()
    try:
        return suggest(connection, {"schema": SCHEMA, "context": context,
                                    "options": [action, "abstain"]})
    finally:
        connection.close()


def _offline_feedback(context: dict, action: str, verifier_state: str,
                      event: dict, locator: dict | None = None,
                      source_app: str | None = None) -> None:
    from offline_policy import SCHEMA, observe
    connection = _offline_policy_connection()
    try:
        feedback = {"schema": SCHEMA, "context": context, "action": action,
                    "outcome": "verified_success" if event["verified"] else "verified_failure",
                    "verifier": {"kind": "ocr", "state": verifier_state, "independent": True},
                    "review_id": event["id"],
                    "rights": {"ownership": "local-use", "training": "local-only",
                               "consent_id": "user.local"}}
        if locator is not None:
            feedback["locator"] = locator
        if source_app in _DOCK_SCOPE:
            feedback["source_app"] = source_app
        observe(connection, feedback)
    finally:
        connection.close()


def _offline_dock_suggestion(target: str, source_app: str | None = None) -> dict:
    from offline_policy import SCHEMA, suggest
    connection = _offline_policy_connection()
    try:
        request = {"schema": SCHEMA, "context": _offline_dock_context(target),
                   "options": ["dock_app_click", "abstain"]}
        if source_app in _DOCK_SCOPE:
            request["source_app"] = source_app
        return suggest(connection, request)
    finally:
        connection.close()


def _valid_dock_point(action: dict, frame: Frame) -> bool:
    return (type(action.get("x")) in (int, float)
            and type(action.get("y")) in (int, float)
            and math.isfinite(action["x"]) and math.isfinite(action["y"])
            and 0 <= action["x"] < frame.width
            and frame.height * 0.8 <= action["y"] < frame.height)


def _offline_dock_feedback(target: str, event: dict, frame: Frame) -> None:
    point = event.get("coordinates")
    locator = ({"x": point["x"] / frame.width, "y": point["y"] / frame.height}
               if event["verified"] and type(point) is dict and _valid_dock_point(point, frame)
               else None)
    _offline_feedback(_offline_dock_context(target), "dock_app_click", "app.frontmost",
                      event, locator, event.get("active_app_before"))


def _offline_sidebar_suggestion(target_view: str) -> dict:
    return _offline_suggestion(_offline_sidebar_context(target_view), "finder_sidebar_click")


def _offline_sidebar_feedback(target_view: str, event: dict) -> None:
    _offline_feedback(_offline_sidebar_context(target_view), "finder_sidebar_click",
                      f"finder.sidebar.{target_view.lower()}.visible", event)


@lru_cache(maxsize=16)
def _ocr_entries(frame: Frame) -> list[dict] | None:
    helper = Path(__file__).with_name("virtualos_stream_server") / "inputctl"
    if not helper.is_file() or not os.access(helper, os.X_OK):
        return None
    with tempfile.NamedTemporaryFile(suffix=".jpg") as image_file:
        image_file.write(frame.jpeg)
        image_file.flush()
        result = subprocess.run([str(helper), "ocr", image_file.name], capture_output=True, text=True, timeout=15)
    if result.returncode:
        return None
    try:
        entries = json.loads(result.stdout)
        return entries if isinstance(entries, list) else None
    except (ValueError, TypeError):
        return None


def _active_guest_app(frame: Frame) -> str | None:
    entries = _ocr_entries(frame)
    if entries is None:
        return None
    try:
        menu = sorted((entry for entry in entries if entry["y"] > 0.94 and 0.035 < entry["x"] < 0.25), key=lambda entry: entry["x"])
        if not menu:
            return None
        name = menu[0]["text"].strip()
        return "Google Chrome" if name == "Chrome" else name
    except (ValueError, KeyError, TypeError):
        return None


def _finder_view_visible(frame: Frame, target: str) -> bool:
    if _active_guest_app(frame) != "Finder":
        return False
    return any(entry.get("text", "").strip() == target
               and 0.20 < entry.get("x", -1) < 0.70
               and 0.80 < entry.get("y", -1) < 0.97
               for entry in (_ocr_entries(frame) or []))


def _finder_sidebar_label(frame: Frame, target: str, entries: list[dict] | None = None) -> dict | None:
    if entries is None:
        entries = _ocr_entries(frame) or []
    labels = [entry for entry in entries
              if re.fullmatch(r"[^A-Za-z]{0,3}" + re.escape(target), entry.get("text", "").strip())
              and 0.03 < entry.get("x", -1) < 0.22
              and 0.20 < entry.get("y", -1) < 0.85]
    return labels[0] if len(labels) == 1 else None


def _finder_label_center(frame: Frame, label: dict) -> tuple[int, int]:
    return (round((label["x"] + label["width"] / 2) * frame.width),
            round((1 - label["y"] - label["height"] / 2) * frame.height))


def _finder_point_in_label(frame: Frame, label: dict, x: int | float, y: int | float) -> bool:
    if (type(x) not in (int, float) or type(y) not in (int, float)
            or not math.isfinite(x) or not math.isfinite(y)):
        return False
    left = label["x"] * frame.width - 30
    right = (label["x"] + label["width"]) * frame.width + 30
    top = (1 - label["y"] - label["height"]) * frame.height - 5
    bottom = (1 - label["y"]) * frame.height + 5
    return left <= x <= right and top <= y <= bottom


def _confirmed_finder_sidebar_click(frame: Frame, action: dict, target: str) -> dict | None:
    if action.get("type") != "click":
        return None
    x, y = action.get("x"), action.get("y")
    label = _finder_sidebar_label(frame, target)
    return action if label and _finder_point_in_label(frame, label, x, y) else None


def _finder_model_action(frame: Frame, target: str, model: str,
                         label: dict | None = None) -> tuple[dict, str | None, str | None]:
    task = f"Click {target} in the Finder sidebar"
    label = label or _finder_sidebar_label(frame, target)
    first = _ollama_action(frame, task, model)
    reason = None
    if first.get("type") == "done" and not _finder_view_visible(frame, target):
        reason = "false_done"
        correction = f" The Finder title does not show {target}; click its visible sidebar label or abstain."
    elif first.get("type") == "click" and label and not _finder_point_in_label(
            frame, label, first.get("x"), first.get("y")):
        reason = "point_outside_label"
        correction = f" The previous point was outside the {target} sidebar label. Click the visible label or abstain."
    else:
        correction = ""
    return (_ollama_action(frame, task + correction, model) if reason else first,
            first.get("type"), reason)


def _guest_lock_screen(frame: Frame) -> tuple[bool, Frame]:
    entries = _ocr_entries(frame) or []
    def matches_lock_screen(items: list[dict]) -> bool:
        has_prompt = any(
            entry.get("text", "").strip().lower() == "enter password"
            and 0.35 <= entry.get("x", -1) <= 0.65
            and 0.06 <= entry.get("y", -1) <= 0.20
            for entry in items
        )
        has_clock = any(
            re.fullmatch(r"\d{1,2}:\d{2}", entry.get("text", "").strip())
            and 0.30 <= entry.get("x", -1) <= 0.70
            and 0.65 <= entry.get("y", -1) <= 0.90
            for entry in items
        )
        return has_prompt and has_clock

    return matches_lock_screen(entries), frame


def _confirmed_dock_click(vm: GuestVM, frame: Frame, action: dict, target: str) -> dict | None:
    """Check the Dock tooltip at and near a model point before clicking."""
    x = round(action["x"])
    y = round(action["y"])
    dock_y = round(frame.height * 0.94)
    points = [(x, y)] + [(x + offset, dock_y) for offset in
                        (-35, 35, -70, 70, -105, 105, -140, 140, -175, 175)]
    deadline = time.monotonic() + 25
    for candidate_x, candidate_y in points:
        if time.monotonic() >= deadline:
            break
        if not (0 <= candidate_x < frame.width and frame.height * 0.80 <= candidate_y < frame.height):
            continue
        try:
            vm.move(candidate_x, candidate_y, frame=frame)
            time.sleep(0.6)
            hovered = vm.frame()
            entries = _ocr_entries(hovered)
        except (OSError, RuntimeError):
            continue
        if entries is None:
            continue
        for entry in entries:
            try:
                label = entry["text"].strip()
                center_x = (entry["x"] + entry["width"] / 2) * frame.width
                if (label == target and 0.10 <= entry["y"] <= 0.25
                        and abs(center_x - candidate_x) <= 90):
                    return {"type": "click", "x": candidate_x, "y": candidate_y}
            except (KeyError, TypeError, ValueError):
                continue
    return None


def run_ollama(vm: GuestVM, task: str, max_steps: int, act: bool, model: str, trace_dir: Path):
    for step in range(max_steps):
        frame = vm.frame()
        target = _dock_target(task)
        target_view = _finder_sidebar_target(task)
        guarded = bool(target or target_view)
        active_before = _active_guest_app(frame) if guarded else None
        locked = False
        if act and guarded and active_before is None:
            locked, frame = _guest_lock_screen(frame)
            if not locked:
                active_before = _active_guest_app(frame)
        view_before = _finder_view_visible(frame, target_view) if target_view and active_before == "Finder" else False
        already_complete = (target is not None and active_before == target) or view_before
        offline_suggestion = None
        if ((target in _DOCK_SCOPE and active_before is not None and not already_complete)
                or (target_view and active_before == "Finder" and not view_before)):
            try:
                offline_suggestion = (_offline_dock_suggestion(target, active_before) if target
                                      else _offline_sidebar_suggestion(target_view))
            except (OSError, ValueError, sqlite3.Error):
                offline_suggestion = None
        risk_counts = ((offline_suggestion.get("source_counts")
                        if offline_suggestion.get("source") == "source_app_verified_feedback"
                        else offline_suggestion["counts"].get("dock_app_click", {"success": 0, "failure": 0}))
                       if offline_suggestion and target else
                       offline_suggestion["counts"].get("finder_sidebar_click", {"success": 0, "failure": 0})
                       if offline_suggestion else {"success": 0, "failure": 0})
        offline_failures = risk_counts["failure"]
        offline_successes = risk_counts["success"]
        offline_bad_history = (offline_failures >= 3 and
                               offline_failures * 5 >= 4 * (offline_failures + offline_successes))
        offline_task = task
        if offline_suggestion and offline_suggestion["choice"] in {"dock_app_click", "finder_sidebar_click"}:
            offline_task += (" The local verified-action policy recommends a Dock click; locate the target icon only."
                             if target else " The local verified-action policy recommends the Finder sidebar click; locate the named row only.")
        replay = None
        if act and target and offline_suggestion and offline_suggestion["choice"] == "dock_app_click":
            locator = offline_suggestion.get("locator")
            if type(locator) is dict:
                candidate = {"type": "click", "x": round(locator["x"] * frame.width),
                             "y": round(locator["y"] * frame.height)}
                if _valid_dock_point(candidate, frame):
                    replay = candidate
        action = ({"type": "abstain", "reason": "guest_locked"} if locked else
                  {"type": "abstain", "reason": "unverified_task"} if act and not guarded else
                  {"type": "abstain", "reason": "guest_desktop_unconfirmed"} if act and guarded and active_before is None else
                  {"type": "abstain", "reason": "finder_not_active"} if act and target_view and active_before != "Finder" else
                  {"type": "done"} if already_complete else
                  {"type": "abstain", "reason": "offline_failure_history"} if act and offline_bad_history else
                  replay if replay is not None else _ollama_action(frame, offline_task, model))
        replay_used = action is replay and replay is not None
        if guarded and action["type"] == "done" and not already_complete:
            action = _ollama_action(frame, task +
                                    f" Current active app: {active_before}. The requested target is not complete.", model)
            if action["type"] == "done":
                action = {"type": "abstain", "reason": "false_done"}
        if guarded and action["type"] not in {"click", "done", "abstain"}:
            action = {"type": "abstain", "reason": "unsupported_guarded_action"}
        elif target and action["type"] == "click" and not _valid_dock_point(action, frame):
            action = {"type": "abstain", "reason": "click_outside_dock_region"}
        proposed_coordinates = {"x": action.get("x"), "y": action.get("y")} if action["type"] == "click" else None
        if act and target and action["type"] == "click":
            confirmed = _confirmed_dock_click(vm, frame, action, target)
            if confirmed is None and replay_used:
                replay_used = False
                fallback = _ollama_action(frame, task, model)
                if fallback.get("type") == "click" and _valid_dock_point(fallback, frame):
                    confirmed = _confirmed_dock_click(vm, frame, fallback, target)
            action = confirmed or {"type": "abstain", "reason": "dock_tooltip_not_confirmed"}
        if act and target_view and action["type"] == "click":
            confirmed = _confirmed_finder_sidebar_click(frame, action, target_view)
            action = confirmed or {"type": "abstain", "reason": "finder_label_not_confirmed"}
        kind = action["type"]
        executed = False
        changed = None
        after = None
        active_after = active_before
        if act and kind not in {"done", "screenshot", "abstain"}:
            foundry_action(vm, action, frame)
            executed = True
            before_hash = hashlib.sha256(frame.jpeg).digest()
            # A Dock hover label changes pixels before the app has opened.
            # Sample until the menu bar confirms the target or the deadline passes.
            for delay in (0.5, 1.0, 2.0, 3.0, 4.0):
                time.sleep(delay)
                after = vm.frame()
                changed = hashlib.sha256(after.jpeg).digest() != before_hash
                if target:
                    active_after = _active_guest_app(after)
                    if active_after == target:
                        break
                elif target_view:
                    active_after = _active_guest_app(after)
                    if _finder_view_visible(after, target_view):
                        break
                elif changed:
                    break
        verified = (active_after == target if target and active_after else
                    _finder_view_visible(after or frame, target_view) if target_view else None)
        event = {
            "id": uuid.uuid4().hex,
            "ts": time.time(),
            "step": step + 1,
            "model": model,
            "task": task,
            "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
            "frame_sha256": hashlib.sha256(frame.jpeg).hexdigest(),
            "action": kind,
            "abstain_reason": action.get("reason") if kind == "abstain" else None,
            "coordinates": {"x": action.get("x"), "y": action.get("y")} if kind in {"click", "double_click"} else None,
            "proposed_coordinates": proposed_coordinates,
            "executed": executed,
            "changed": changed,
            "target_app": target or ("Finder" if target_view else None),
            "target_view": target_view,
            "action_source": "model_label_guarded" if target_view and executed else
                             "offline_locator_tooltip_guarded" if target and executed and replay_used else
                             "model_tooltip_guarded" if target and executed else None,
            "active_app_before": active_before,
            "active_app_after": active_after,
            "verified": verified,
            "mode": "act" if act else "shadow",
            "offline_choice": offline_suggestion["choice"] if offline_suggestion else None,
        }
        _record_local_step(trace_dir, frame, event, after)
        if target in _DOCK_SCOPE and executed and kind == "click" and type(verified) is bool:
            try:
                _offline_dock_feedback(target, event, frame)
            except (OSError, ValueError, sqlite3.Error) as error:
                print(f"offline_learning=failed: {type(error).__name__}", file=sys.stderr, flush=True)
        elif target_view and executed and kind == "click" and type(verified) is bool:
            try:
                _offline_sidebar_feedback(target_view, event)
            except (OSError, ValueError, sqlite3.Error) as error:
                print(f"offline_learning=failed: {type(error).__name__}", file=sys.stderr, flush=True)
        complete = (kind == "done" and verified is True) or (executed and verified is True)
        status = "complete" if complete else "abstain" if kind == "abstain" else event["mode"]
        print(json.dumps({"status": status, "step": step + 1, "action": kind,
                          "abstain_reason": event["abstain_reason"],
                          "coordinates": event["coordinates"], "executed": executed,
                          "changed": changed, "verified": verified,
                          "active_app": active_after}), flush=True)
        if complete or kind in {"done", "abstain"} or not act:
            return
    print(json.dumps({"status": "step_limit", "steps": max_steps}), flush=True)


def run_nova_act(vm: GuestVM, task: str, starting_page: str, max_steps: int):
    """Expose guest controls to Nova Act's browser workflow as local tools."""
    if not os.getenv("NOVA_ACT_API_KEY"):
        raise RuntimeError("NOVA_ACT_API_KEY is required for the Nova Act adapter")
    from nova_act import NovaAct, tool  # Optional SDK.

    @tool
    def guest_status() -> dict:
        """Return the connected virtualOS guest health and screenshot dimensions.

        Returns:
            dict: Guest readiness and screenshot dimensions.
        """
        frame = vm.frame()
        return {"ready": True, "width": frame.width, "height": frame.height}

    @tool
    def guest_observe(question: str) -> str:
        """Use Nova 2 Lite vision to answer a question about the virtualOS guest screenshot.

        Args:
            question (str): What to identify or read on the guest screen.
        Returns:
            str: Visible evidence from the guest screenshot.
        """
        import boto3

        frame = vm.frame()
        client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
        response = client.converse(
            modelId=os.getenv("NOVA_MODEL_ID", "us.amazon.nova-2-lite-v1:0"),
            messages=[{"role": "user", "content": [
                {"text": f"Guest screenshot is {frame.width}x{frame.height} pixels, origin top left. {question} Give concise visible evidence and pixel coordinates where relevant."},
                {"image": {"format": "jpeg", "source": {"bytes": frame.jpeg}}},
            ]}],
            inferenceConfig={"maxTokens": 400, "temperature": 0},
        )
        return "".join(block.get("text", "") for block in response["output"]["message"]["content"])

    @tool
    def guest_click(x: int, y: int) -> str:
        """Click the virtualOS guest at top-left-origin screenshot coordinates.

        Args:
            x (int): Horizontal pixel coordinate in the guest screenshot.
            y (int): Vertical pixel coordinate in the guest screenshot.
        Returns:
            str: Confirmation of the guest click.
        """
        vm.click(x, y)
        return "Guest click completed"

    @tool
    def guest_type(text: str) -> str:
        """Enter text into the currently focused virtualOS guest control.

        Args:
            text (str): Text to enter in the guest.
        Returns:
            str: Confirmation without echoing the text.
        """
        vm.type(text)
        return "Text entered in guest"

    @tool
    def guest_key(key: str) -> str:
        """Press one guest key such as return, tab, escape, or a single letter.

        Args:
            key (str): Guest key to press.
        Returns:
            str: Confirmation of the guest key press.
        """
        vm.key(key)
        return "Guest key pressed"

    with NovaAct(starting_page=starting_page, headless=True, tools=[guest_status, guest_observe, guest_click, guest_type, guest_key]) as nova:
        result = nova.act(
            "Operate the connected virtualOS guest with the guest_* tools. "
            "Call guest_observe to inspect the guest before choosing coordinates, "
            "and inspect it again after each guest action. Task: " + task,
            max_steps=max_steps,
        )
        print(result)


async def run_fara(vm: GuestVM, task: str, max_steps: int):
    """Run the official Fara1.5 agent against guest Chrome using its environment API."""
    from fara.agents.fara.fara15_agent import Fara15Agent, Fara15AgentConfig
    from fara.core.data_point import Task, SolverStatus, UserMessage, UserMessageType
    from fara.core.run_context import RunContext
    from fara.environments.computer import BrowserEnvironment, BrowserEnvironmentConfig, OSType, PageContext

    class VMEnvironment(BrowserEnvironment):
        @classmethod
        def _get_config_class(cls):
            return BrowserEnvironmentConfig

        @property
        def os_type(self):
            return OSType.MACOS

        async def initialize(self, **kwargs):
            vm.health()
            self._initialized = True

        async def close(self):
            self._initialized = False

        async def is_alive(self):
            return bool(vm.health()["ready"])

        async def get_observation(self):
            return vm.frame().jpeg

        async def get_screenshot(self):
            return vm.frame().jpeg

        async def left_click(self, x, y):
            vm.click(x, y)

        async def right_click(self, x, y):
            vm.click(x, y, button="right")

        async def double_click(self, x, y):
            vm.click(x, y, count=2)

        async def triple_click(self, x, y):
            vm.click(x, y, count=3)

        async def mouse_move(self, x, y):
            vm.move(x, y)

        async def left_click_drag(self, end_x, end_y):
            vm.drag(end_x, end_y)

        async def key(self, keys):
            vm.key(keys)

        async def type(self, text):
            vm.type(text)

        async def scroll(self, pixels):
            vm.scroll(pixels)

        async def hscroll(self, pixels):
            vm.scroll(0, horizontal=pixels)

        async def wait(self, duration):
            await asyncio.sleep(min(max(float(duration), 0), 10))

        async def get_page_context(self):
            return PageContext(url="", page_info="Guest Chrome browser")

        async def get_page_markdown(self):
            raise RuntimeError("Page markdown is unavailable through guest pixels")

        async def goto_url(self, url):
            vm.key(["command", "l"])
            vm.type(url)
            vm.key("return")

        async def go_back(self):
            vm.key(["command", "["])

        async def refresh(self):
            vm.key(["command", "r"])

    frame = vm.frame()
    config_path = os.getenv("FARA_ENDPOINT_CONFIG_PATH")
    config = json.loads(Path(config_path).read_text() if config_path else os.environ["FARA_ENDPOINT_CONFIG"])
    env = VMEnvironment(viewport_width=frame.width, viewport_height=frame.height)
    agent = Fara15Agent(Fara15AgentConfig(client_config=config, max_rounds=max_steps, identity="fara_qwen35", critical_points="fara-1.5", captcha_timeout_limit=0, viewport_width=frame.width, viewport_height=frame.height))
    await env.initialize()
    output_dir = Path(os.getenv("VOS_CUA_OUTPUT", "/tmp/virtualos-cua")) / ("fara-" + uuid.uuid4().hex[:8])
    context = RunContext.create(environment=env, task=Task(task_id=uuid.uuid4().hex, instruction=task), output_dir=output_dir)
    try:
        await agent.initialize(context)
        answer, _, _ = await agent.run(context)
        while context.solver_log.status == SolverStatus.WAITING_FOR_USER:
            print(json.dumps({"status": "input_required", "question": answer}))
            if not sys.stdin.isatty():
                return
            reply = input("Your response (Enter to stop): ").strip()
            if not reply:
                return
            context.add_observation(UserMessage(content=reply, message_type=UserMessageType.CRITICAL_POINT_RESPONSE))
            answer, _, _ = await agent.run(context)
        print(json.dumps({"status": context.solver_log.status.value, "answer": answer}))
    finally:
        await agent.close(context)
        await env.close()


def probe_guest_tree(vm: GuestVM) -> dict:
    """Probe guest AX roles over the NAT bridge; retain no names or values."""
    transport_only = os.getenv("VOS_GUEST_TREE_TRANSPORT_ONLY") == "1"
    counts_only = os.getenv("VOS_GUEST_AX_COUNTS") == "1"
    symbolic_state = os.getenv("VOS_GUEST_AX_STATE", "")
    if symbolic_state not in {"", "finder_window_present"}:
        raise ValueError("unsupported symbolic guest AX state")
    frame = vm.frame()
    if _active_guest_app(frame) != "Terminal":
        return {"guest_tree": "unavailable", "reason": "guest_terminal_not_active"}
    token = uuid.uuid4().hex
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("192.168.64.1", 40819))
        listener.listen(1)
        listener.settimeout(35)
        # Return only AX role names or the numeric AppleScript error. Error
        # descriptions may contain window titles or private UI text.
        ax = ('tell application "System Events"\n'
              'try\n'
              'return role of every UI element of menu bar 1 of application process "Terminal"\n'
              'on error errMsg number errNum\n'
              'return "AX_ERROR_" & (errNum as text)\n'
              'end try\n'
              'end tell')
        if counts_only:
            ax = ('tell application "System Events"\n'
                  'try\n'
                  'set p to count of (application processes whose name is "Terminal")\n'
                  'set w to -999\n'
                  'set m to -999\n'
                  'if p is 1 then\n'
                  'try\n'
                  'set w to count of windows of application process "Terminal"\n'
                  'on error errMsg number errNum\n'
                  'set w to errNum\n'
                  'end try\n'
                  'try\n'
                  'set m to count of menu bars of application process "Terminal"\n'
                  'on error errMsg number errNum\n'
                  'set m to errNum\n'
                  'end try\n'
                  'end if\n'
                  'return "AX_COUNTS_" & p & "_" & w & "_" & m\n'
                  'on error errMsg number errNum\n'
                  'return "AX_ERROR_" & (errNum as text)\n'
                  'end try\n'
                  'end tell')
        if symbolic_state == "finder_window_present":
            ax = ('tell application "System Events"\n'
                  'try\n'
                  'set p to count of (application processes whose name is "Finder")\n'
                  'set w to 0\n'
                  'if p is 1 then set w to count of windows of application process "Finder"\n'
                  'return "AX_FINDER_" & p & "_" & w\n'
                  'on error errMsg number errNum\n'
                  'return "AX_ERROR_" & (errNum as text)\n'
                  'end try\n'
                  'end tell')
        source_command = ("/usr/bin/printf AX_NET_OK" if transport_only else
                          "( /usr/bin/osascript -e " + shlex.quote(ax) +
                          " 2>/dev/null || /usr/bin/printf AX_SCRIPT_ERROR )")
        guest_command = (source_command + " | "
                         "/usr/bin/curl -fsS --max-time 8 -X POST --data-binary @- "
                         "http://192.168.64.1:40819/" + token + " >/dev/null\n")
        encoded_path, script_path = run_guest_hex_script(vm, guest_command.encode("utf-8"))
        try:
            connection, address = listener.accept()
            with connection:
                connection.settimeout(5)
                if address[0] != "192.168.64.2":
                    raise RuntimeError("guest tree sender was not the guest NAT address")
                request = bytearray()
                while b"\r\n\r\n" not in request and len(request) < 4096:
                    chunk = connection.recv(4096 - len(request))
                    if not chunk:
                        raise RuntimeError("guest tree request ended early")
                    request.extend(chunk)
                headers, separator, prefix = bytes(request).partition(b"\r\n\r\n")
                if not separator or b"POST /" + token.encode() + b" HTTP/1.1" not in headers.split(b"\r\n", 1)[0]:
                    raise RuntimeError("guest tree request was invalid")
                match = re.search(rb"(?im)^content-length:\s*([0-9]+)\s*$", headers)
                if not match or int(match.group(1)) > 4096:
                    raise RuntimeError("guest tree payload exceeded limit")
                length = int(match.group(1))
                body = bytearray(prefix)
                while len(body) < length:
                    chunk = connection.recv(length - len(body))
                    if not chunk:
                        raise RuntimeError("guest tree payload ended early")
                    body.extend(chunk)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
        finally:
            vm._request("/vm/exec", {"cmd": f"rm -f {encoded_path} {script_path}", "paste": True})
    data = body[:length].decode("utf-8", errors="replace").strip()
    if transport_only:
        return {"guest_tree_transport": "passed" if data == "AX_NET_OK" else "rejected",
                "image_retained": False, "guest_text_retained": False}
    if counts_only:
        match = re.fullmatch(r"AX_COUNTS_([0-9]{1,3})_(-?[0-9]{1,6})_(-?[0-9]{1,6})", data)
        if match:
            process_count, window_value, menu_value = (int(value) for value in match.groups())
            result = {"guest_ax_counts": "measured", "terminal_process_count": process_count,
                      "image_retained": False, "guest_text_retained": False}
            if process_count == 1:
                result["window_count" if window_value >= 0 else "window_error"] = window_value
                result["menu_bar_count" if menu_value >= 0 else "menu_bar_error"] = menu_value
                if window_value == -25211 or menu_value == -25211:
                    result["accessibility_api_disabled"] = True
            return result
        if re.fullmatch(r"AX_ERROR_-?[0-9]{1,6}", data):
            return {"guest_ax_counts": "unavailable", "apple_script_error": int(data[9:])}
        return {"guest_ax_counts": "rejected", "reason": "unexpected_numeric_payload"}
    if symbolic_state == "finder_window_present":
        match = re.fullmatch(r"AX_FINDER_([0-9]{1,3})_([0-9]{1,4})", data)
        if match:
            process_count, window_count = (int(value) for value in match.groups())
            return {"symbolic_state": symbolic_state, "observation": "measured",
                    "verified": process_count == 1 and window_count > 0,
                    "process_count": process_count, "window_count": window_count,
                    "frontmost_independent": True, "names_retained": False,
                    "values_retained": False, "image_retained": False}
        if re.fullmatch(r"AX_ERROR_-?[0-9]{1,6}", data):
            return {"symbolic_state": symbolic_state, "observation": "unavailable",
                    "apple_script_error": int(data[9:])}
        return {"symbolic_state": symbolic_state, "observation": "rejected",
                "reason": "unexpected_numeric_payload"}
    if data.startswith("AX_ERROR_") and re.fullmatch(r"AX_ERROR_-?[0-9]{1,6}", data):
        return {"guest_tree": "unavailable", "surface": "terminal_menu_bar",
                "reason": "guest_ax_error", "apple_script_error": int(data[9:])}
    if data == "AX_SCRIPT_ERROR":
        return {"guest_tree": "unavailable", "surface": "terminal_menu_bar",
                "reason": "guest_ax_script_failed"}
    if not data:
        return {"guest_tree": "unavailable", "surface": "terminal_menu_bar",
                "reason": "guest_ax_empty"}
    roles = [part.strip() for part in data.split(",")]
    if len(roles) > 128 or any(not re.fullmatch(r"[A-Za-z ]{1,40}", role) for role in roles):
        return {"guest_tree": "rejected", "surface": "terminal_menu_bar",
                "reason": "unexpected_role_payload"}
    counts = {role: roles.count(role) for role in sorted(set(roles))}
    return {"guest_tree": "roles_only", "surface": "terminal_menu_bar", "role_counts": counts, "names_retained": False,
            "values_retained": False, "image_retained": False}


def guest_ax_delayed_focus(vm: GuestVM) -> dict:
    """One-shot background AX sample after a reviewed Finder Dock click."""
    frame = vm.frame()
    if _active_guest_app(frame) != "Terminal":
        return {"background_ax": "refused", "reason": "terminal_not_active"}
    point = _confirmed_dock_click(
        vm, frame,
        {"type": "click", "x": round(frame.width * 0.333), "y": round(frame.height * 0.94)},
        "Finder",
    )
    if point is None or _active_guest_app(vm.frame()) != "Terminal":
        return {"background_ax": "refused", "reason": "finder_dock_target_not_confirmed"}
    nonce = uuid.uuid4().hex
    ax = ('tell application "System Events"\n'
          'try\n'
          'set p to count of (application processes whose name is "Finder")\n'
          'if p is not 1 then return "AX_BG_0_0"\n'
          'set f to frontmost of application process "Finder"\n'
          'set w to count of windows of application process "Finder"\n'
          'if f then return "AX_BG_1_" & w\n'
          'return "AX_BG_0_" & w\n'
          'on error errMsg number errNum\n'
          'return "AX_ERROR_" & (errNum as text)\n'
          'end try\n'
          'end tell')
    source = ("if [ \"$1\" != go ]; then sh \"$0\" go >/dev/null 2>&1 & exit; fi\n"
              "sleep 6\n"
              "( /usr/bin/osascript -e " + shlex.quote(ax) +
              " 2>/dev/null || /usr/bin/printf AX_SCRIPT_ERROR ) | "
              "/usr/bin/curl -fsS --max-time 8 -X POST --data-binary @- "
              "http://192.168.64.1:40819/" + nonce + " >/dev/null\n")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("192.168.64.1", 40819))
        listener.listen(1)
        listener.settimeout(20)
        run_guest_hex_script(vm, source.encode("utf-8"), self_clean=True)
        vm.click(point["x"], point["y"], frame=frame)
        time.sleep(1)
        if _active_guest_app(vm.frame()) != "Finder":
            return {"background_ax": "unverified", "reason": "finder_did_not_become_active"}
        connection, peer = listener.accept()
        with connection:
            connection.settimeout(5)
            if peer[0] != "192.168.64.2":
                raise RuntimeError("background AX sender was not the guest")
            received = bytearray()
            while b"\r\n\r\n" not in received and len(received) < 2048:
                chunk = connection.recv(2048 - len(received))
                if not chunk:
                    raise RuntimeError("background AX request ended early")
                received.extend(chunk)
            headers, sep, prefix = bytes(received).partition(b"\r\n\r\n")
            if not sep or headers.split(b"\r\n", 1)[0] != b"POST /" + nonce.encode() + b" HTTP/1.1":
                raise RuntimeError("background AX request path invalid")
            match = re.search(rb"(?im)^content-length:\s*([0-9]+)\s*$", headers)
            if not match or int(match.group(1)) > 128:
                raise RuntimeError("background AX payload invalid")
            length = int(match.group(1))
            body = bytearray(prefix)
            while len(body) < length:
                chunk = connection.recv(length - len(body))
                if not chunk:
                    raise RuntimeError("background AX payload ended early")
                body.extend(chunk)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
    value = body[:length].decode("ascii", errors="replace").strip()
    match = re.fullmatch(r"AX_BG_([01])_([0-9]{1,4})", value)
    if match:
        return {"background_ax": "measured", "nonce": nonce,
                "finder_frontmost": match.group(1) == "1", "finder_window_count": int(match.group(2)),
                "names_retained": False, "values_retained": False, "image_retained": False}
    if re.fullmatch(r"AX_ERROR_-?[0-9]{1,6}", value):
        return {"background_ax": "unavailable", "nonce": nonce,
                "apple_script_error": int(value[9:])}
    return {"background_ax": "rejected", "nonce": nonce, "reason": "unexpected_numeric_payload"}


def run_guest_hex_script(vm: GuestVM, source: bytes, *, self_clean: bool = False) -> tuple[str, str]:
    """Type lowercase hex and decode it in the guest using unshifted keys only."""
    if not 1 <= len(source) <= 2048 or b"\x00" in source:
        raise ValueError("guest bootstrap script size or content invalid")
    name = "voshex" + uuid.uuid4().hex[:8]
    encoded_path = "/private/tmp/" + name
    script_path = encoded_path + "script"
    if self_clean:
        source = source.rstrip(b"\n") + b"\n" + f"rm -f {encoded_path} {script_path}\n".encode("ascii")
    encoded = source.hex()
    # Bound each /vm/exec typing request. A long hex line can overrun its
    # request timeout and leave a half-entered command in Terminal.
    commands = []
    offset = 0
    for start in range(0, len(encoded), 384):
        chunk = encoded[start:start + 384]
        commands.extend((
            f"dd bs=1 count={len(chunk) + 1} seek={offset} of={encoded_path} conv=notrunc",
            chunk,
        ))
        offset += len(chunk) + 1
    commands.extend((f"xxd -r -p {encoded_path} {script_path}", f"sh {script_path}"))
    for command in commands:
        if not re.fullmatch(r"[a-z0-9 /=-]+", command):
            raise ValueError("guest bootstrap contains a shifted key")
        vm._request("/vm/exec", {"cmd": command, "paste": True})
        time.sleep(0.8)
    return encoded_path, script_path


def guest_hex_smoke(vm: GuestVM) -> dict:
    """Bootstrap a harmless guest script using only unshifted key characters."""
    if _active_guest_app(vm.frame()) != "Terminal":
        return {"guest_hex_smoke": "unavailable", "reason": "guest_terminal_not_active"}
    token = uuid.uuid4().hex
    source = b""
    if os.getenv("VOS_GUEST_LONG_SMOKE") == "1":
        source = b"#" + b"a" * 600 + b"\n"
    source += ("/usr/bin/printf hexok | /usr/bin/curl -fsS --max-time 8 "
               "-X POST --data-binary @- http://192.168.64.1:40820/" +
               token + " >/dev/null\n").encode("ascii")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("192.168.64.1", 40820))
        listener.listen(1)
        listener.settimeout(15)
        run_guest_hex_script(vm, source, self_clean=True)
        try:
            connection, address = listener.accept()
        except socket.timeout:
            return {"guest_hex_smoke": "unverified", "reason": "callback_timeout",
                    "script_bytes": len(source), "shifted_keys_sent": 0,
                    "image_retained": False}
        with connection:
            connection.settimeout(5)
            if address[0] != "192.168.64.2":
                raise RuntimeError("guest smoke sender was not the guest NAT address")
            request = bytearray()
            while len(request) < 4096 and b"\r\n\r\n" not in request:
                part = connection.recv(4096 - len(request))
                if not part:
                    break
                request.extend(part)
            headers, separator, body = bytes(request).partition(b"\r\n\r\n")
            expected = b"POST /" + token.encode("ascii") + b" HTTP/1.1"
            length = re.search(rb"(?im)^content-length:\s*([0-9]+)\s*$", headers)
            if not separator or headers.split(b"\r\n", 1)[0] != expected or not length or int(length.group(1)) != 5:
                raise RuntimeError("guest smoke callback was invalid")
            while len(body) < 5:
                part = connection.recv(5 - len(body))
                if not part:
                    raise RuntimeError("guest smoke callback ended early")
                body += part
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
    return {"guest_hex_smoke": "passed" if body == b"hexok" else "unverified",
            "script_bytes": len(source), "shifted_keys_sent": 0,
            "image_retained": False}


def guest_accessibility_open(vm: GuestVM) -> dict:
    """Open the guest's supported Accessibility privacy pane from Terminal."""
    if _active_guest_app(vm.frame()) != "Terminal":
        return {"guest_accessibility_ui": "unavailable", "reason": "guest_terminal_not_active"}
    command = b"open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'\n"
    run_guest_hex_script(vm, command, self_clean=True)
    for delay in (1, 2, 3):
        time.sleep(delay)
        frame = vm.frame(allow_blank=True)
        if not frame.likely_blank and _active_guest_app(frame) == "System Settings":
            return {"guest_accessibility_ui": "opened", "image_retained": False}
    return {"guest_accessibility_ui": "unverified", "image_retained": False}


def guest_accessibility_authorize(vm: GuestVM) -> dict:
    """Submit the configured guest password only at the exact macOS consent prompt."""
    frame = vm.frame()
    if _active_guest_app(frame) != "System Settings":
        return {"guest_accessibility_authorization": "refused", "reason": "system_settings_not_active"}
    entries = _ocr_entries(frame) or []
    visible = " ".join(str(row.get("text", "")) for row in entries).casefold()
    checks = {"privacy_security": "privacy & security" in visible,
              "modify_settings": "modify settings" in visible,
              "password_instruction": "enter your password" in visible,
              "guest_account_ai": re.search(r"\bai\b", visible) is not None}
    # The macOS consent modal covers the Terminal row. Its identity must be
    # checked on the preceding visible pane and explicitly attested here.
    if not all(checks.values()) or os.getenv("VOS_GUEST_AX_TERMINAL_CONFIRMED") != "1":
        return {"guest_accessibility_authorization": "refused",
                "reason": "exact_prompt_or_terminal_attestation_missing", "visible_checks": checks}
    password_row = next((row for row in entries if str(row.get("text", "")).strip().casefold() == "password"), None)
    button_row = next((row for row in entries if str(row.get("text", "")).strip().casefold() == "modify settings"), None)
    if not password_row or not button_row:
        return {"guest_accessibility_authorization": "refused", "reason": "prompt_controls_not_located"}
    password = os.getenv("VOS_VM_PASSWORD", "")
    if not password:
        path = Path.home() / ".config/virtualos/start.env"
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.match(r"^\s*(?:export\s+)?VOS_VM_PASSWORD=(.*)$", line)
                if match:
                    password = match.group(1).strip().strip("\"'")
                    break
    if not re.fullmatch(r"[a-z0-9]{1,64}", password):
        return {"guest_accessibility_authorization": "refused", "reason": "supported_guest_password_unavailable"}

    def center(row: dict) -> tuple[float, float]:
        return ((float(row["x"]) + float(row["width"]) / 2) * frame.width,
                (1 - float(row["y"]) - float(row["height"]) / 2) * frame.height)

    vm.click(*center(password_row), frame=frame)
    for char in password:
        vm.key(char)
    vm.click(*center(button_row), frame=frame)
    dismissed = False
    for delay in (1, 2, 3):
        time.sleep(delay)
        after = vm.frame(allow_blank=True)
        if after.likely_blank:
            continue
        after_text = " ".join(str(row.get("text", "")) for row in (_ocr_entries(after) or [])).casefold()
        dismissed = "modify settings" not in after_text and _active_guest_app(after) == "System Settings"
        if dismissed:
            break
    return {"guest_accessibility_authorization": "submitted" if dismissed else "unverified",
            "prompt_dismissed": dismissed,
            "image_retained": False, "password_retained": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Control the virtualOS guest with a computer-use model")
    parser.add_argument("provider", choices=["doctor", "audit", "review", "guest-tree", "guest-ax-delayed-focus", "guest-hex-smoke", "guest-accessibility-open", "guest-accessibility-authorize", "foundry", "nova2", "nova-act", "fara", "ollama"])
    parser.add_argument("task", nargs="?", help="Task for the selected provider")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--starting-page", default="about:blank", help="Nova Act's own browser start page")
    parser.add_argument("--act", action="store_true", help="Execute local model actions inside the guest; default is shadow observation")
    parser.add_argument("--model", default=os.getenv("VOS_OLLAMA_MODEL", "qwen3.5:0.8b"))
    parser.add_argument("--trace-dir", type=Path, default=Path.home() / "Library/Application Support/virtualOS/computer-use")
    parser.add_argument("--decision", choices=["accept", "reject"], help="Visual review decision for a trace ID")
    args = parser.parse_args()
    if args.max_steps < 1 or args.max_steps > 100:
        parser.error("--max-steps must be from 1 to 100")
    if args.provider not in {"doctor", "audit", "guest-tree", "guest-ax-delayed-focus", "guest-hex-smoke", "guest-accessibility-open", "guest-accessibility-authorize"} and not args.task:
        parser.error("a task is required")
    if args.provider == "audit":
        print(json.dumps(audit_local_steps(args.trace_dir)))
        return 0
    if args.provider == "review":
        if not args.decision:
            parser.error("review requires --decision accept or reject")
        print(json.dumps(review_local_step(args.trace_dir, args.task, args.decision)))
        return 0
    vm = GuestVM()
    if args.provider == "guest-tree":
        result = probe_guest_tree(vm)
        print(json.dumps(result), flush=True)
        if result.get("symbolic_state"):
            return 0 if result.get("verified") is True else 1
        return 0 if (result.get("guest_tree") == "roles_only" or
                     result.get("guest_tree_transport") == "passed" or
                     result.get("guest_ax_counts") == "measured") else 1
    if args.provider == "guest-ax-delayed-focus":
        result = guest_ax_delayed_focus(vm)
        print(json.dumps(result), flush=True)
        return 0 if result.get("background_ax") == "measured" and result.get("finder_frontmost") is True else 1
    if args.provider == "guest-hex-smoke":
        result = guest_hex_smoke(vm)
        print(json.dumps(result), flush=True)
        return 0 if result["guest_hex_smoke"] == "passed" else 1
    if args.provider == "guest-accessibility-open":
        result = guest_accessibility_open(vm)
        print(json.dumps(result), flush=True)
        return 0 if result["guest_accessibility_ui"] == "opened" else 1
    if args.provider == "guest-accessibility-authorize":
        result = guest_accessibility_authorize(vm)
        print(json.dumps(result), flush=True)
        return 0 if result["guest_accessibility_authorization"] == "submitted" else 1
    if args.provider == "doctor":
        frame = vm.frame(allow_blank=True)
        active_app = _active_guest_app(frame) if not frame.likely_blank else None
        print(json.dumps({
            "ready": True,
            "screenshot_usable": not frame.likely_blank,
            "active_app": active_app,
            "guest_locked": _guest_lock_screen(frame)[0] if not frame.likely_blank and active_app is None else False,
            "width": frame.width,
            "height": frame.height,
            "jpeg_bytes": len(frame.jpeg),
            "provider_setup": {
                "foundry": {"endpoint_set": bool(os.getenv("FOUNDRY_PROJECT_ENDPOINT")), "auth_source_present": bool(os.getenv("FOUNDRY_TOKEN") or shutil.which("az"))},
                "nova2": {"boto3_installed": importlib.util.find_spec("boto3") is not None},
                "nova_act": {"sdk_installed": importlib.util.find_spec("nova_act") is not None, "api_key_set": bool(os.getenv("NOVA_ACT_API_KEY")), "boto3_installed": importlib.util.find_spec("boto3") is not None},
                "fara": {"sdk_installed": importlib.util.find_spec("fara") is not None, "endpoint_config_set": bool(os.getenv("FARA_ENDPOINT_CONFIG_PATH") or os.getenv("FARA_ENDPOINT_CONFIG"))},
            },
        }))
    elif args.provider == "foundry":
        run_foundry(vm, args.task, args.max_steps)
    elif args.provider == "nova2":
        run_bedrock(vm, args.task, args.max_steps)
    elif args.provider == "nova-act":
        run_nova_act(vm, args.task, args.starting_page, args.max_steps)
    elif args.provider == "ollama":
        run_ollama(vm, args.task, args.max_steps, args.act, args.model, args.trace_dir)
    else:
        asyncio.run(run_fara(vm, args.task, args.max_steps))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ModuleNotFoundError) as exc:
        print(f"Missing provider setup: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"Computer use failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
