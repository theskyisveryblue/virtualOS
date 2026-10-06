#!/usr/bin/env python3
"""Offline multi-brain fleet runner.

Runs one local model "brain" per virtual machine, concurrently, against the
headless FleetSupervisor. No cloud relay, no host Accessibility/Screen
Recording: perception comes from captured guest frames (OCR via the local
inputctl helper) and action goes through the supervisor's per-VM input queue,
which synthesizes events inside a hidden VZVirtualMachineView.

    tools/fleet/brain.py doctor
    tools/fleet/brain.py run --task "Click the Finder icon in the Dock" --vms vm-a,vm-b
    tools/fleet/brain.py run --task ... --vms vm-a --max-steps 6 --act

Design goals (from the project directives):
  * offline-first   - every dependency is localhost; cloud is optional
  * many brains      - N agents, one per VM, run in parallel threads
  * independent      - each brain keeps its own frame history and trace
  * honest           - an action is only "verified" when the guest state changed
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SUPERVISOR = os.environ.get("VOS_FLEET_SUPERVISOR", "http://127.0.0.1:9225")
DEFAULT_MODEL = os.environ.get("VOS_OLLAMA_MODEL", "qwen3.5:0.8b")
DEFAULT_OLLAMA = os.environ.get("VOS_OLLAMA_URL", "http://127.0.0.1:11434")
_OCR_HELPER = Path(__file__).resolve().parent.parent / "virtualos_stream_server" / "inputctl"


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    width: int
    height: int

    @property
    def likely_blank(self) -> bool:
        return len(self.jpeg) < max(8000, min(50000, int(self.width * self.height * 0.02)))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.jpeg).hexdigest()


def _http(method: str, url: str, body: bytes | None = None,
          ctype: str = "application/json", timeout: float = 15.0):
    headers = {"Connection": "close", "User-Agent": "virtualos-fleet-brain/1.0"}
    if body is not None:
        headers["Content-Type"] = ctype
        headers["Content-Length"] = str(len(body))
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


class FleetVM:
    """One fleet VM, adapted to the single-guest GuestVM contract.

    Talks only to the local supervisor. Coordinates are top-left origin in the
    captured frame, matching how models reason about screenshots.
    """

    def __init__(self, name: str, supervisor: str = DEFAULT_SUPERVISOR):
        self.name = name
        self.supervisor = supervisor.rstrip("/")
        self._ocr_cache: tuple[str, list[dict]] | None = None

    # -- perception --------------------------------------------------------

    def health(self) -> dict:
        with _http("GET", f"{self.supervisor}/fleet/status") as resp:
            status = json.load(resp)
        instances = {inst.get("name"): inst for inst in status.get("instances", [])}
        inst = instances.get(self.name)
        if not inst:
            raise RuntimeError(f"unknown vm {self.name}")
        if not inst.get("ready"):
            raise RuntimeError(f"vm {self.name} not ready ({inst.get('state')})")
        return inst

    def frame(self, tries: int = 3) -> Frame:
        last: Frame | None = None
        for attempt in range(tries):
            with _http("GET", f"{self.supervisor}/fleet/frame/{self.name}", timeout=20.0) as resp:
                jpeg = resp.read()
                width = int(resp.headers["X-Image-Width"])
                height = int(resp.headers["X-Image-Height"])
            frame = Frame(jpeg, width, height)
            if not frame.likely_blank:
                return frame
            last = frame
            time.sleep(0.5 * (attempt + 1))
        return last if last is not None else Frame(b"", 0, 0)

    def ocr(self, frame: Frame) -> list[dict]:
        """Vision OCR of a captured frame via the local helper. No host screen."""
        if self._ocr_cache is not None and self._ocr_cache[0] == frame.sha256:
            return self._ocr_cache[1]
        if not _OCR_HELPER.is_file() or not os.access(_OCR_HELPER, os.X_OK):
            return []
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            tmp.write(frame.jpeg)
            tmp.flush()
            result = subprocess.run([str(_OCR_HELPER), "ocr-fast", tmp.name],
                                    capture_output=True, text=True, timeout=15)
        if result.returncode:
            return []
        try:
            entries = json.loads(result.stdout)
        except (ValueError, TypeError):
            return []
        if not isinstance(entries, list):
            return []
        self._ocr_cache = (frame.sha256, entries)
        return entries

    # -- action ------------------------------------------------------------

    def _input(self, payload: dict) -> int:
        body = json.dumps(payload).encode("utf-8")
        try:
            with _http("POST", f"{self.supervisor}/fleet/input/{self.name}", body) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except urllib.error.URLError:
            return 0

    def click(self, x: float, y: float) -> int:
        return self._input({"action": "click", "nx": x, "ny": y})

    def move(self, x: float, y: float) -> int:
        return self._input({"action": "move", "nx": x, "ny": y})

    def key(self, name: str) -> int:
        return self._input({"action": "key", "key": name})

    def type(self, text: str) -> int:
        return self._input({"action": "type", "text": text})


# --------------------------------------------------------------------------
# local brain (offline model)
# --------------------------------------------------------------------------

def _ollama_action(frame: Frame, task: str, model: str, endpoint: str) -> dict:
    prompt = (
        f"Task: {task}\n"
        "Look at this macOS screenshot. Find the visible UI target for the task. "
        "Return ONE JSON object. To click use {\"type\":\"click\",\"point_2d\":[x,y],\"label\":name} "
        "where point_2d is a 0-1000 scale from the top left. "
        "To press a key use {\"type\":\"key\",\"key\":\"return\"} (one of return, escape, tab, "
        "space, delete, up, down, left, right). "
        "To type text into a focused field use {\"type\":\"type\",\"text\":\"...\"}. "
        "If the target is not visible return {\"type\":\"abstain\"}. "
        "Only return {\"type\":\"done\"} if the task is visibly complete. Never guess a point."
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(frame.jpeg).decode("ascii")]}],
        "format": "json", "stream": False, "think": False,
        "keep_alive": os.environ.get("VOS_OLLAMA_KEEP_ALIVE", "60s"),
        "options": {"temperature": 0, "num_predict": 256},
    }).encode("utf-8")
    with _http("POST", endpoint.rstrip("/") + "/api/chat", body, timeout=120.0) as resp:
        payload = json.load(resp)
    raw = payload.get("message", {}).get("content", "") or ""
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    candidate = (fenced.group(1) if fenced else raw).strip()
    if candidate and candidate[0] not in "[{":
        starts = [i for i in (candidate.find("{"), candidate.find("[")) if i >= 0]
        candidate = candidate[min(starts):] if starts else candidate
    try:
        action, _ = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("local model returned invalid JSON") from exc
    if isinstance(action, list) and len(action) == 1:
        action = action[0]
    if not isinstance(action, dict):
        return {"type": "abstain", "reason": "non_object_action"}
    if "point_2d" in action:
        point = action["point_2d"]
        if (not isinstance(point, list) or len(point) != 2
                or not all(isinstance(v, (int, float)) and 0 <= v <= 1000 for v in point)):
            return {"type": "abstain", "reason": "bad_point"}
        return {"type": "click", "point_2d": point, "label": action.get("label")}
    if action.get("type") == "key":
        key = action.get("key")
        if not isinstance(key, str) or not key or len(key) > 24:
            return {"type": "abstain", "reason": "bad_key"}
        return {"type": "key", "key": key.lower()}
    if action.get("type") == "type":
        text = action.get("text")
        if not isinstance(text, str) or not text or len(text) > 512:
            return {"type": "abstain", "reason": "bad_text"}
        return {"type": "type", "text": text}
    return action


# --------------------------------------------------------------------------
# one brain loop
# --------------------------------------------------------------------------

@dataclass
class BrainResult:
    vm: str
    status: str
    steps: int
    actions: list = field(default_factory=list)
    error: str | None = None

    def to_json(self) -> dict:
        out = {"vm": self.vm, "status": self.status, "steps": self.steps,
               "actions": self.actions}
        if self.error:
            out["error"] = self.error
        return out


def run_brain(vm: FleetVM, task: str, model: str, endpoint: str,
              max_steps: int, act: bool, trace_dir: Path) -> BrainResult:
    """Perceive -> decide -> act -> observe for one VM. Independent state."""
    result = BrainResult(vm=vm.name, status="step_limit", steps=0)
    trace = trace_dir / vm.name
    try:
        vm.health()
    except (RuntimeError, urllib.error.URLError) as exc:
        result.status = "unavailable"
        result.error = str(exc)
        return result
    for step in range(1, max_steps + 1):
        frame = vm.frame()
        if not frame.jpeg:
            result.status = "blank_frame"
            return result
        before = frame.sha256
        try:
            action = _ollama_action(frame, task, model, endpoint)
        except (ValueError, urllib.error.URLError) as exc:
            result.status = "model_error"
            result.error = str(exc)
            return result
        kind = action.get("type")
        if kind == "done":
            result.status = "done"
            result.steps = step
            _record(trace, vm.name, step, task, "done", None, before, before, None)
            return result
        if kind == "abstain":
            result.status = "abstain"
            result.steps = step
            _record(trace, vm.name, step, task, "abstain", None, before, before, None)
            return result
        if kind not in ("click", "key", "type") or not act:
            result.status = "shadow" if not act else "unsupported"
            result.steps = step
            return result
        if kind == "click":
            px = round(action["point_2d"][0] * (frame.width - 1) / 1000)
            py = round(action["point_2d"][1] * (frame.height - 1) / 1000)
            status = vm.click(px, py)
            detail = {"point": [px, py]}
        elif kind == "key":
            status = vm.key(action["key"])
            detail = {"key": action["key"]}
        else:
            status = vm.type(action["text"])
            detail = {"text_len": len(action["text"])}
        changed = None
        after_hash = before
        for delay in (0.4, 0.8, 1.6):
            time.sleep(delay)
            after = vm.frame()
            after_hash = after.sha256
            changed = after_hash != before
            if changed:
                break
        result.actions.append({"step": step, "action": kind,
                               **detail, "http": status, "changed": changed})
        _record(trace, vm.name, step, task, kind, detail.get("point"),
                before, after_hash, changed)
        if changed:
            result.status = "acted"
            result.steps = step
            return result
    result.steps = max_steps
    return result


def _record(trace: Path, vm: str, step: int, task: str, action: str,
            point, before: str, after: str, changed) -> None:
    trace.mkdir(parents=True, exist_ok=True, mode=0o700)
    row = {"vm": vm, "step": step, "ts": time.time(),
           "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
           "action": action, "point": point,
           "frame_before": before, "frame_after": after, "changed": changed}
    with (trace / "brain.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


# --------------------------------------------------------------------------
# concurrent fleet of brains
# --------------------------------------------------------------------------

def run_fleet(vms: list[str], task: str, supervisor: str, model: str,
              endpoint: str, max_steps: int, act: bool,
              trace_dir: Path, workers: int) -> list[BrainResult]:
    brains = [FleetVM(name, supervisor) for name in vms]
    results: list[BrainResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_brain, vm, task, model, endpoint,
                               max_steps, act, trace_dir): vm.name for vm in brains}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item.vm)
    return results


def _supervisor_status(supervisor: str) -> dict:
    try:
        with _http("GET", f"{supervisor}/fleet/status", timeout=10.0) as resp:
            return json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError):
        return {}


def _ollama_models(endpoint: str) -> list[str]:
    try:
        with _http("GET", endpoint.rstrip("/") + "/api/tags", timeout=10.0) as resp:
            payload = json.load(resp)
        return [m.get("name") for m in payload.get("models", [])]
    except (urllib.error.URLError, urllib.error.HTTPError):
        return []


def cmd_doctor(args) -> int:
    status = _supervisor_status(args.supervisor)
    instances = status.get("instances", [])
    models = _ollama_models(args.endpoint)
    report = {
        "supervisor": args.supervisor,
        "supervisor_reachable": bool(status),
        "vms": [{"name": inst.get("name"), "ready": inst.get("ready"),
                 "state": inst.get("state")} for inst in instances],
        "ollama": args.endpoint,
        "model": args.model,
        "model_present": args.model in models,
        "ocr_helper": _OCR_HELPER.is_file() and os.access(_OCR_HELPER, os.X_OK),
        "offline": True,
        "cloud_required": False,
    }
    print(json.dumps(report, indent=2))
    ok = bool(status) and report["model_present"] and report["ocr_helper"]
    return 0 if ok else 1


def cmd_run(args) -> int:
    vms = [name.strip() for name in args.vms.split(",") if name.strip()]
    trace_dir = args.trace_dir
    results = run_fleet(vms, args.task, args.supervisor, args.model,
                        args.endpoint, args.max_steps, args.act,
                        trace_dir, args.workers)
    for result in results:
        print(json.dumps(result.to_json()), flush=True)
    return 0 if all(r.status in {"done", "acted", "shadow", "abstain"} for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain", description="Offline multi-brain fleet runner")
    parser.add_argument("--supervisor", default=DEFAULT_SUPERVISOR)
    parser.add_argument("--endpoint", default=DEFAULT_OLLAMA)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--trace-dir", type=Path,
                        default=Path.home() / "Library/Application Support/virtualOS/brain")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check offline readiness (supervisor, model, OCR)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="run one brain per VM, concurrently")
    p.add_argument("--task", required=True)
    p.add_argument("--vms", required=True, help="comma-separated VM names")
    p.add_argument("--max-steps", type=int, default=4)
    p.add_argument("--workers", type=int, default=0, help="0 = one per VM")
    p.add_argument("--act", action="store_true", help="execute clicks in the guest")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= getattr(args, "max_steps", 1) <= 100:
        print("--max-steps must be 1..100", file=sys.stderr)
        return 2
    if getattr(args, "workers", 0) == 0 and getattr(args, "vms", ""):
        args.workers = max(1, len(args.vms.split(",")))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
