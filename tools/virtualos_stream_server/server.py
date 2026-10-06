#!/usr/bin/env python3
import argparse
import atexit
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ALLOWED_APPS = {
    "Codex",
    "ChatGPT",
    "virtualOS",
    "Simulator",
    "Finder",
    "Figma",
    "Terminal",
    "Google Chrome",
}

ALLOWED_URL_SCHEMES = {"http", "https"}
DEFAULT_OPEN_ROOTS = [
    Path.home() / "Documents",
    Path.home() / "Desktop",
    Path.home() / "Downloads",
    Path.home() / "Applications",
    Path("/Applications"),
]
DEFAULT_WATCH_REPO_PATHS = [
    Path.home() / "Documents/GitHub/virtualOS",
    Path.home() / "Documents/GitHub/ai-share",
]

INPUTCTL_PATH = Path(__file__).with_name("inputctl")

KEY_CODES = {
    "enter": 36,
    "tab": 48,
    "escape": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "delete": 51,
}

ALLOWED_MODIFIERS = {"command", "cmd", "shift", "control", "ctrl", "option", "alt"}

DISPLAY_BOUNDS_LOCK = threading.Lock()
DISPLAY_BOUNDS_CACHE: tuple[int, int, int, int] | None = None
DISPLAY_RECTS_CACHE: list[tuple[int, int, int, int]] | None = None
DISPLAY_IDS_CACHE: list[int] | None = None
MAIN_DISPLAY_ID_CACHE: int | None = None
DISPLAY_BOUNDS_CACHE_AT = 0.0
DISPLAY_BOUNDS_CACHE_TTL_SECONDS = 3.0
REQUIRED_AUTH_TOKEN = ""
VM_BUNDLE_DIR = Path.home() / "Library/Containers/com.github.yep.ios.virtualOS/Data/Documents"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def resolve_watch_repo_paths() -> list[Path]:
    raw = os.getenv("VOS_WATCH_REPOS", "").strip()
    if not raw:
        return _dedupe_paths(DEFAULT_WATCH_REPO_PATHS)

    normalized = raw.replace("\n", ",").replace(";", ",")
    paths = [Path(item.strip()).expanduser() for item in normalized.split(",") if item.strip()]
    return _dedupe_paths(paths) or _dedupe_paths(DEFAULT_WATCH_REPO_PATHS)


def path_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def allowed_open_roots() -> list[Path]:
    roots = [root.expanduser() for root in DEFAULT_OPEN_ROOTS]
    roots.extend(resolve_watch_repo_paths())
    return _dedupe_paths(roots)


def run_process(cmd: list[str], timeout_seconds: float = 3.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False, f"command timeout after {timeout_seconds}s"
    except Exception as exc:
        return False, str(exc)
    ok = proc.returncode == 0
    out = (proc.stdout or proc.stderr or "").strip()
    return ok, out


class VMLogMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries: deque[dict] = deque(maxlen=3000)
        self._seq = 0
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._status = "not-started"
        self._recent_failure_cache: list[str] = []
        self._recent_failure_cache_at = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="vm-log-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self._proc = None

    def _add(self, line: str) -> None:
        line = line.rstrip("\n")
        if not line:
            return
        if self._should_skip_line(line):
            return
        with self._lock:
            self._seq += 1
            self._entries.append(
                {
                    "seq": self._seq,
                    "ts": int(time.time()),
                    "line": line,
                }
            )

    @staticmethod
    def _should_skip_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        # Ignore huge IOHID dumps that flood log views with numeric service IDs.
        if "[com.apple.iohid:oversized]" in line or "IOHIDSetModifierLockState" in line:
            return True
        if stripped in {"{", "}", "services =     (", "virtualServices =     (", ");"}:
            return True
        if stripped.endswith(","):
            maybe_num = stripped[:-1].strip()
            if maybe_num.isdigit():
                return True
        return False

    def _run_loop(self) -> None:
        predicate = (
            'process == "virtualOS" OR process == "virtualizationd" OR '
            'process == "com.apple.Virtualization.EventTap" OR process == "com.apple.Virtualization.VirtualMachine" OR '
            'process == "launchd" OR process == "deleted" OR subsystem == "com.apple.Virtualization"'
        )
        while self._running:
            cmd = [
                "/usr/bin/log",
                "stream",
                "--style",
                "compact",
                "--level",
                "debug",
                "--predicate",
                predicate,
            ]
            self._status = "connecting"
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self._proc = proc
                self._status = "streaming"
                self._add("vm-log-monitor: started log stream")

                if not proc.stdout:
                    self._status = "no-stdout"
                    self._add("vm-log-monitor: no stdout from log stream")
                    time.sleep(1.0)
                    continue

                for line in proc.stdout:
                    if not self._running:
                        break
                    self._add(line)

                rc = proc.poll()
                if self._running:
                    self._status = f"restarting (log stream exited {rc})"
                    self._add(f"vm-log-monitor: log stream exited ({rc}), restarting...")
                    time.sleep(1.0)
            except Exception as exc:
                self._status = "error"
                self._add(f"vm-log-monitor: stream error: {exc}")
                time.sleep(2.0)
            finally:
                self._proc = None

    def snapshot(self, since_seq: int = 0, limit: int = 400) -> tuple[int, list[dict]]:
        with self._lock:
            last_seq = self._seq
            if since_seq > 0:
                items = [e for e in self._entries if int(e.get("seq", 0)) > since_seq]
            else:
                items = list(self._entries)[-limit:]
        return last_seq, items

    def _find_latest_bundle(self) -> Path | None:
        try:
            if not VM_BUNDLE_DIR.exists():
                return None
            bundles = sorted(
                [p for p in VM_BUNDLE_DIR.glob("*.bundle") if p.is_dir()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return bundles[0] if bundles else None
        except Exception:
            return None

    def _bundle_stats(self, bundle: Path | None) -> dict:
        if not bundle:
            return {"bundlePath": "", "exists": False}

        disk_img = bundle / "Disk.img"
        out: dict = {"bundlePath": str(bundle), "exists": bundle.exists()}
        if disk_img.exists():
            try:
                st = disk_img.stat()
                out["diskLogicalBytes"] = st.st_size
                out["diskLogicalGB"] = round(st.st_size / (1024**3), 2)
                allocated = st.st_blocks * 512
                out["diskAllocatedBytes"] = allocated
                out["diskAllocatedGB"] = round(allocated / (1024**3), 2)
            except Exception:
                pass
        params_path = bundle / "Parameters.txt"
        if params_path.exists():
            try:
                params = json.loads(params_path.read_text())
                out["params"] = params
            except Exception:
                out["params"] = {}
        return out

    def _recent_failure_lines(self) -> list[str]:
        now = time.monotonic()
        if self._recent_failure_cache and (now - self._recent_failure_cache_at) < 60:
            return self._recent_failure_cache
        patterns = (
            "guest did stop",
            "internal virtualization error",
            "virtual machine stopped unexpectedly",
            "os_reason_jetsam",
            "running failed",
            "critical low disk",
        )
        lines: list[str] = []
        with self._lock:
            for entry in self._entries:
                line = str(entry.get("line", "")).strip()
                if not line:
                    continue
                lower = line.lower()
                if any(p in lower for p in patterns):
                    lines.append(line)
        lines = lines[-40:]

        # Fallback to a short historical scan only if live buffer has no failures yet.
        if not lines:
            cmd = [
                "/usr/bin/log",
                "show",
                "--last",
                "30m",
                "--style",
                "compact",
                "--predicate",
                'eventMessage CONTAINS[c] "Guest did stop" OR '
                'eventMessage CONTAINS[c] "Internal Virtualization error" OR '
                'eventMessage CONTAINS[c] "virtual machine stopped unexpectedly" OR '
                'eventMessage CONTAINS[c] "OS_REASON_JETSAM" OR '
                'eventMessage CONTAINS[c] "running failed" OR '
                'eventMessage CONTAINS[c] "critical low disk"',
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
                raw = (proc.stdout or "").splitlines()
                lines = [ln for ln in raw if ln.strip()][-40:]
            except Exception:
                lines = []
        self._recent_failure_cache = lines
        self._recent_failure_cache_at = now
        return lines

    def status(self) -> dict:
        data_total, data_used, data_free = shutil.disk_usage("/System/Volumes/Data")
        bundle = self._find_latest_bundle()
        return {
            "ok": True,
            "monitorStatus": self._status,
            "lastSeq": self._seq,
            "hostDataVolume": {
                "totalGB": round(data_total / (1024**3), 2),
                "usedGB": round(data_used / (1024**3), 2),
                "freeGB": round(data_free / (1024**3), 2),
                "usedPct": round((data_used / max(data_total, 1)) * 100, 2),
            },
            "vmBundle": self._bundle_stats(bundle),
            "recentFailureLines": self._recent_failure_lines(),
            "time": int(time.time()),
        }


VM_LOG_MONITOR = VMLogMonitor()


def run_git(repo_path: Path, args: list[str], timeout_seconds: float = 8.0) -> tuple[bool, str]:
    return run_process(["git", "-C", str(repo_path), *args], timeout_seconds=timeout_seconds)


def collect_repo_status(repo_path: Path, fetch_remote: bool = True) -> dict:
    repo = repo_path.expanduser()
    payload = {
        "name": repo.name,
        "path": str(repo),
        "exists": repo.exists(),
        "ok": False,
        "branch": "",
        "head": "",
        "headShort": "",
        "upstream": "",
        "upstreamHead": "",
        "upstreamHeadShort": "",
        "dirty": False,
        "ahead": 0,
        "behind": 0,
        "updateAvailable": False,
        "error": "",
        "fetched": False,
    }

    if not repo.exists():
        payload["error"] = "missing"
        return payload

    ok, out = run_git(repo, ["rev-parse", "--is-inside-work-tree"])
    if not ok or out.strip() != "true":
        payload["error"] = out or "not a git repo"
        return payload

    ok, out = run_git(repo, ["branch", "--show-current"])
    if ok:
        payload["branch"] = out.strip()

    ok, out = run_git(repo, ["rev-parse", "HEAD"])
    if ok:
        payload["head"] = out.strip()
        payload["headShort"] = payload["head"][:12]

    ok, out = run_git(repo, ["status", "--porcelain"])
    if ok:
        payload["dirty"] = bool(out.strip())

    ok, upstream = run_git(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not ok:
        payload["error"] = upstream or "no upstream"
        return payload

    payload["upstream"] = upstream.strip()
    if fetch_remote:
        fetch_ok, fetch_out = run_git(repo, ["fetch", "--prune", "--quiet"], timeout_seconds=15.0)
        payload["fetched"] = fetch_ok
        if not fetch_ok:
            payload["error"] = fetch_out or "fetch failed"

    ok, out = run_git(repo, ["rev-parse", "@{u}"])
    if ok:
        payload["upstreamHead"] = out.strip()
        payload["upstreamHeadShort"] = payload["upstreamHead"][:12]

    ok, out = run_git(repo, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
    if ok:
        parts = out.split()
        if len(parts) == 2:
            try:
                payload["ahead"] = int(parts[0])
                payload["behind"] = int(parts[1])
                payload["updateAvailable"] = payload["behind"] > 0
            except ValueError:
                payload["error"] = "bad ahead/behind data"

    payload["ok"] = payload["error"] == ""
    return payload


class RepoStatusMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._repos = resolve_watch_repo_paths()
        self._poll_interval_seconds = max(60, int(os.getenv("VOS_REPO_POLL_INTERVAL", "300") or "300"))
        self._running = False
        self._thread: threading.Thread | None = None
        self._status = "not-started"
        self._last_checked_at = 0.0
        self._snapshots: list[dict] = []

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.refresh(fetch_remote=False)
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="repo-status-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run_loop(self) -> None:
        while self._running:
            self.refresh(fetch_remote=True)
            remaining = self._poll_interval_seconds
            while self._running and remaining > 0:
                time.sleep(min(1.0, remaining))
                remaining -= 1.0

    def refresh(self, fetch_remote: bool = True) -> None:
        snapshots = [collect_repo_status(repo, fetch_remote=fetch_remote) for repo in self._repos]
        with self._lock:
            self._snapshots = snapshots
            self._last_checked_at = time.time()
            if any(item.get("updateAvailable") for item in snapshots):
                self._status = "updates-available"
            elif any(not item.get("ok") for item in snapshots):
                self._status = "degraded"
            else:
                self._status = "ok"

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "monitorStatus": self._status,
                "pollIntervalSeconds": self._poll_interval_seconds,
                "lastCheckedAt": int(self._last_checked_at) if self._last_checked_at else 0,
                "repos": [dict(item) for item in self._snapshots],
                "time": int(time.time()),
            }

    def watched_repo_paths(self) -> list[str]:
        with self._lock:
            return [str(path) for path in self._repos]

    def pull_repo(self, repo_ref: str) -> tuple[bool, dict]:
        requested = repo_ref.strip()
        if not requested:
            return False, {"ok": False, "error": "missing repo"}

        with self._lock:
            repo_paths = [Path(item) for item in self._repos]

        target: Path | None = None
        for repo_path in repo_paths:
            if requested == str(repo_path) or requested == repo_path.name:
                target = repo_path
                break

        if target is None:
            return False, {"ok": False, "error": f"repo '{requested}' is not watched"}

        before = collect_repo_status(target, fetch_remote=True)
        if not before.get("exists"):
            return False, {"ok": False, "error": "repo missing", "repo": before}
        if not before.get("ok"):
            return False, {"ok": False, "error": before.get("error") or "repo not ready", "repo": before}
        if before.get("dirty"):
            return False, {"ok": False, "error": "repo has uncommitted changes", "repo": before}

        upstream = str(before.get("upstream", "")).strip()
        if not upstream:
            return False, {"ok": False, "error": "repo has no upstream", "repo": before}

        pull_ok, pull_out = run_git(target, ["pull", "--ff-only"], timeout_seconds=20.0)
        after = collect_repo_status(target, fetch_remote=False)
        self.refresh(fetch_remote=False)

        return pull_ok, {
            "ok": pull_ok,
            "result": pull_out or ("already up to date" if before.get("behind", 0) == 0 else "pulled"),
            "before": before,
            "repo": after,
        }


REPO_STATUS_MONITOR = RepoStatusMonitor()


def get_local_ipv4_addresses() -> list[str]:
    candidates: set[str] = set()
    for iface in ("en0", "en1", "en2", "bridge100", "awdl0", "eth0", "wlan0"):
        try:
            proc = subprocess.run(
                ["/usr/sbin/ipconfig", "getifaddr", iface],
                capture_output=True,
                text=True,
                timeout=0.6,
            )
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        value = (proc.stdout or "").strip()
        if value and value != "127.0.0.1":
            candidates.add(value)

    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in ips:
            if ip and ip != "127.0.0.1":
                candidates.add(ip)
    except Exception:
        pass

    return sorted(candidates)


class BonjourAdvertiser:
    def __init__(self, port: int):
        self.port = port
        self.process: subprocess.Popen | None = None
        self.service_name = f"virtualOS-{socket.gethostname().split('.')[0]}"

    def start(self) -> tuple[bool, str]:
        dns_sd = shutil.which("dns-sd")
        if not dns_sd:
            return False, "dns-sd not found; Bonjour advertise disabled"

        try:
            self.process = subprocess.Popen(
                [dns_sd, "-R", self.service_name, "_virtualosremote._tcp", "local.", str(self.port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self.process = None
            return False, f"bonjour advertise failed: {exc}"
        return True, f"{self.service_name}._virtualosremote._tcp.local:{self.port}"

    def stop(self) -> None:
        if not self.process:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=1.0)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        finally:
            self.process = None


def run_applescript(
    lines: list[str],
    argv: list[str] | None = None,
    timeout_seconds: float = 3.0,
) -> tuple[bool, str]:
    cmd = ["osascript"]
    for line in lines:
        cmd.extend(["-e", line])
    if argv:
        cmd.extend(argv)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False, f"applescript timeout after {timeout_seconds}s"
    ok = proc.returncode == 0
    out = (proc.stdout or proc.stderr or "").strip()
    return ok, out


def run_inputctl(args: list[str], timeout_seconds: float = 2.0) -> tuple[bool, str]:
    if not INPUTCTL_PATH.exists():
        return False, f"inputctl missing at {INPUTCTL_PATH}"
    cmd = [str(INPUTCTL_PATH), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False, f"inputctl timeout after {timeout_seconds}s"
    ok = proc.returncode == 0
    out = (proc.stdout or proc.stderr or "").strip()
    return ok, out


def get_display_union_bounds() -> tuple[int, int, int, int] | None:
    global DISPLAY_BOUNDS_CACHE, DISPLAY_RECTS_CACHE, DISPLAY_IDS_CACHE, MAIN_DISPLAY_ID_CACHE, DISPLAY_BOUNDS_CACHE_AT
    with DISPLAY_BOUNDS_LOCK:
        now = time.monotonic()
        if DISPLAY_BOUNDS_CACHE and (now - DISPLAY_BOUNDS_CACHE_AT) <= DISPLAY_BOUNDS_CACHE_TTL_SECONDS:
            return DISPLAY_BOUNDS_CACHE

        script = r"""
import CoreGraphics
var ids = Array(repeating: CGDirectDisplayID(0), count: 16)
var count: UInt32 = 0
let err = CGGetActiveDisplayList(16, &ids, &count)
if err != .success || count == 0 {
    print("")
    exit(0)
}
print("M:\(CGMainDisplayID())")
var minX = Double.greatestFiniteMagnitude
var minY = Double.greatestFiniteMagnitude
var maxX = -Double.greatestFiniteMagnitude
var maxY = -Double.greatestFiniteMagnitude
var displayLines: [String] = []
for i in 0..<Int(count) {
    let b = CGDisplayBounds(ids[i])
    displayLines.append("\(ids[i]),\(Int(b.origin.x)),\(Int(b.origin.y)),\(Int(b.size.width)),\(Int(b.size.height))")
    minX = min(minX, b.minX)
    minY = min(minY, b.minY)
    maxX = max(maxX, b.maxX)
    maxY = max(maxY, b.maxY)
}
print("U:\(Int(minX)),\(Int(minY)),\(Int(maxX - minX)),\(Int(maxY - minY))")
for line in displayLines {
    print("D:\(line)")
}
"""
        try:
            proc = subprocess.run(["swift", "-e", script], capture_output=True, text=True, timeout=2.0)
        except subprocess.TimeoutExpired:
            return DISPLAY_BOUNDS_CACHE

        if proc.returncode != 0:
            return DISPLAY_BOUNDS_CACHE

        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        main_line = next((ln for ln in lines if ln.startswith("M:")), "")
        union_line = next((ln for ln in lines if ln.startswith("U:")), "")
        display_lines = [ln[2:] for ln in lines if ln.startswith("D:")]
        if not union_line:
            return DISPLAY_BOUNDS_CACHE
        parts = union_line[2:].split(",")
        if len(parts) != 4:
            return DISPLAY_BOUNDS_CACHE
        try:
            min_x, min_y, width, height = [int(p.strip()) for p in parts]
        except ValueError:
            return DISPLAY_BOUNDS_CACHE
        if width <= 0 or height <= 0:
            return DISPLAY_BOUNDS_CACHE
        rects: list[tuple[int, int, int, int]] = []
        ids: list[int] = []
        for line in display_lines:
            ps = line.split(",")
            did: int | None = None
            if len(ps) == 5:
                try:
                    did = int(ps[0].strip())
                    x, y, w, h = [int(p.strip()) for p in ps[1:5]]
                except ValueError:
                    continue
            elif len(ps) == 4:
                try:
                    x, y, w, h = [int(p.strip()) for p in ps]
                except ValueError:
                    continue
            else:
                continue
            if w > 0 and h > 0:
                rects.append((x, y, w, h))
                if did is not None:
                    ids.append(did)

        main_id: int | None = None
        if main_line:
            try:
                main_id = int(main_line[2:].strip())
            except ValueError:
                main_id = None

        DISPLAY_BOUNDS_CACHE = (min_x, min_y, width, height)
        DISPLAY_RECTS_CACHE = rects if rects else None
        DISPLAY_IDS_CACHE = ids if ids and len(ids) == len(rects) else None
        MAIN_DISPLAY_ID_CACHE = main_id
        DISPLAY_BOUNDS_CACHE_AT = now
        return DISPLAY_BOUNDS_CACHE


def get_display_rects() -> list[tuple[int, int, int, int]] | None:
    _ = get_display_union_bounds()
    return DISPLAY_RECTS_CACHE


def get_display_ids() -> list[int] | None:
    _ = get_display_union_bounds()
    return DISPLAY_IDS_CACHE


def get_main_display_id() -> int | None:
    _ = get_display_union_bounds()
    return MAIN_DISPLAY_ID_CACHE


def clamp_float(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def point_in_rect(x: int, y: int, rect: tuple[int, int, int, int]) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x <= (rx + rw - 1) and ry <= y <= (ry + rh - 1)


def project_point_to_rect(x: int, y: int, rect: tuple[int, int, int, int]) -> tuple[int, int]:
    rx, ry, rw, rh = rect
    px = clamp_int(x, rx, rx + rw - 1)
    py = clamp_int(y, ry, ry + rh - 1)
    return px, py


def project_point_to_active_display(x: int, y: int, rects: list[tuple[int, int, int, int]]) -> tuple[int, int, bool]:
    for rect in rects:
        if point_in_rect(x, y, rect):
            return x, y, False

    best = (x, y)
    best_dist2 = None
    for rect in rects:
        px, py = project_point_to_rect(x, y, rect)
        dx = px - x
        dy = py - y
        dist2 = dx * dx + dy * dy
        if best_dist2 is None or dist2 < best_dist2:
            best = (px, py)
            best_dist2 = dist2
    return best[0], best[1], True


def rect_overlap_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def choose_display_for_capture(capture_app: str = "virtualOS") -> tuple[int | None, tuple[int, int, int, int], str] | None:
    rects = get_display_rects() or []
    if not rects:
        return None

    ids = get_display_ids() or []
    main_id = get_main_display_id()

    chosen_index = 0
    reason = "fallback-first"
    if main_id is not None and ids:
        try:
            chosen_index = ids.index(main_id)
            reason = "main-display"
        except ValueError:
            chosen_index = 0

    app = capture_app.strip()
    if app:
        win = get_app_window_rect(app)
        if win:
            best_overlap = 0
            best_idx = None
            for idx, rect in enumerate(rects):
                overlap = rect_overlap_area(win, rect)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_idx = idx
            if best_idx is not None and best_overlap > 0:
                chosen_index = best_idx
                reason = f"app-overlap:{app}"
            else:
                cx = win[0] + win[2] // 2
                cy = win[1] + win[3] // 2
                best_dist2 = None
                nearest_idx = None
                for idx, rect in enumerate(rects):
                    px, py = project_point_to_rect(cx, cy, rect)
                    dx = px - cx
                    dy = py - cy
                    dist2 = dx * dx + dy * dy
                    if best_dist2 is None or dist2 < best_dist2:
                        best_dist2 = dist2
                        nearest_idx = idx
                if nearest_idx is not None:
                    chosen_index = nearest_idx
                    reason = f"app-nearest:{app}"

    chosen_index = clamp_int(chosen_index, 0, len(rects) - 1)
    chosen_rect = rects[chosen_index]
    chosen_id = ids[chosen_index] if chosen_index < len(ids) else None
    return chosen_id, chosen_rect, reason


def map_normalized_click_to_quartz(
    nx: float, ny: float, source: str, target_app: str = "virtualOS"
) -> tuple[bool, tuple[int, int] | None, str]:
    nx = clamp_float(nx, 0.0, 1.0)
    ny = clamp_float(ny, 0.0, 1.0)

    source = source.strip().lower()
    if source == "fullscreen":
        source = "screen"
    if source not in {"window", "screen", "all"}:
        return False, None, f"unsupported source '{source}'"

    bounds = get_display_union_bounds()
    if not bounds:
        return False, None, "display bounds unavailable"
    min_x, min_y, width, height = bounds
    max_y = min_y + height

    if source == "window":
        rect = get_app_window_rect(target_app)
        if not rect:
            return False, None, f"window for app '{target_app}' not found"
        win_x, win_y, win_w, win_h = rect
        # window rect uses top-left coordinates; convert to global Quartz Y.
        top_x = win_x + int(round(nx * max(win_w - 1, 0)))
        top_y = win_y + int(round(ny * max(win_h - 1, 0)))
        quartz_x = top_x
        quartz_y = max_y - top_y
        return True, (quartz_x, quartz_y), f"window:{target_app}:{win_x},{win_y},{win_w},{win_h}"

    if source == "screen":
        chosen = choose_display_for_capture(target_app)
        if chosen:
            _, (sx, sy, sw, sh), reason = chosen
            top_x = sx + int(round(nx * max(sw - 1, 0)))
            top_y = sy + int(round(ny * max(sh - 1, 0)))
            quartz_x = top_x
            quartz_y = max_y - top_y
            return True, (quartz_x, quartz_y), f"screen:{reason}:{sx},{sy},{sw},{sh}"

    # all-desktop capture uses union bounds; map normalized top-left to Quartz.
    raw_x = min_x + int(round(nx * max(width - 1, 0)))
    raw_y = min_y + int(round((1.0 - ny) * max(height - 1, 0)))
    rects = get_display_rects() or []
    if rects:
        quartz_x, quartz_y, projected = project_point_to_active_display(raw_x, raw_y, rects)
        projection_note = ";projected" if projected else ""
    else:
        quartz_x, quartz_y = raw_x, raw_y
        projection_note = ""
    return True, (quartz_x, quartz_y), f"screen:{min_x},{min_y},{width},{height}{projection_note}"


def get_app_window_rect(app_name: str) -> tuple[int, int, int, int] | None:
    if not app_name.strip():
        return None
    script = [
        "on run argv",
        "set appName to item 1 of argv",
        'tell application "System Events"',
        "  if not (exists process appName) then",
        '    return ""',
        "  end if",
        "  tell process appName",
        "    if (count of windows) = 0 then",
        '      return ""',
        "    end if",
        '    set frontRect to ""',
        "    try",
        "      set p to position of window 1",
        "      set s to size of window 1",
        "      set ww to item 1 of s",
        "      set hh to item 2 of s",
        "      if ww > 0 and hh > 0 then",
        '        set frontRect to (item 1 of p as string) & "," & (item 2 of p as string) & "," & (ww as string) & "," & (hh as string)',
        "      end if",
        "    end try",
        '    if frontRect is not "" then',
        "      return frontRect",
        "    end if",
        "    set bestArea to 0",
        '    set bestRect to ""',
        "    repeat with w in windows",
        "      set p to position of w",
        "      set s to size of w",
        "      set ww to item 1 of s",
        "      set hh to item 2 of s",
        "      set area to ww * hh",
        "      if area > bestArea then",
        "        set bestArea to area",
        '        set bestRect to (item 1 of p as string) & "," & (item 2 of p as string) & "," & (ww as string) & "," & (hh as string)',
        "      end if",
        "    end repeat",
        "    return bestRect",
        "  end tell",
        "end tell",
        "end run",
    ]
    ok, out = run_applescript(script, argv=[app_name], timeout_seconds=2.0)
    if not ok or not out:
        return None

    parts = out.split(",")
    if len(parts) != 4:
        return None

    try:
        x, y, w, h = [int(p.strip()) for p in parts]
    except ValueError:
        return None

    if w <= 0 or h <= 0:
        return None

    return (x, y, w, h)


def get_virtualos_window_rect() -> tuple[int, int, int, int] | None:
    return get_app_window_rect("virtualOS")


def run_screencapture(cmd: list[str], target_file: Path, mode: str) -> tuple[bool, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0 and target_file.exists() and target_file.stat().st_size > 0:
        return True, mode
    return False, (proc.stderr or proc.stdout or "capture failed").strip()


def read_frame_bytes(target_file: Path, max_dim: int = 0) -> tuple[bool, bytes, str]:
    if max_dim <= 0:
        try:
            return True, target_file.read_bytes(), ""
        except Exception as exc:
            return False, b"", f"frame read failed: {exc}"

    resized = target_file.with_name(f"{target_file.stem}_max{max_dim}.jpg")
    cmd = [
        "/usr/bin/sips",
        "-s",
        "format",
        "jpeg",
        "--resampleHeightWidthMax",
        str(max_dim),
        str(target_file),
        "--out",
        str(resized),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not resized.exists() or resized.stat().st_size <= 0:
        # Fall back to original image if resize fails.
        try:
            return True, target_file.read_bytes(), ";scaled=skip"
        except Exception as exc:
            err = (proc.stderr or proc.stdout or "").strip()
            if err:
                return False, b"", f"resize failed: {err}"
            return False, b"", f"frame read failed: {exc}"
    try:
        return True, resized.read_bytes(), f";scaled=max{max_dim}"
    except Exception as exc:
        return False, b"", f"resized frame read failed: {exc}"


def capture_frame(target_file: Path, source: str = "window", capture_app: str = "virtualOS") -> tuple[bool, str]:
    target_file.parent.mkdir(parents=True, exist_ok=True)
    source = source.strip().lower()
    if source == "fullscreen":
        source = "screen"
    if source not in {"window", "screen", "all"}:
        source = "window"
    last_error = "capture failed"

    if source == "window":
        rect = get_app_window_rect(capture_app)
        if rect:
            x, y, w, h = rect
            cmd = ["/usr/sbin/screencapture", "-x", f"-R{x},{y},{w},{h}", str(target_file)]
            ok, out = run_screencapture(cmd, target_file, f"rect:{capture_app}:{x},{y},{w},{h}")
            if ok:
                return True, out
            last_error = out
        source = "screen"

    if source == "screen":
        chosen = choose_display_for_capture(capture_app)
        if chosen:
            display_id, (x, y, w, h), reason = chosen
            if display_id is not None:
                cmd = ["/usr/sbin/screencapture", "-x", "-D", str(display_id), str(target_file)]
                ok, out = run_screencapture(cmd, target_file, f"screen:{reason}:display={display_id}")
                if ok:
                    return True, out
                last_error = out
            cmd = ["/usr/sbin/screencapture", "-x", f"-R{x},{y},{w},{h}", str(target_file)]
            ok, out = run_screencapture(cmd, target_file, f"screen:{reason}:rect={x},{y},{w},{h}")
            if ok:
                return True, out
            last_error = out
        source = "all"

    cmd = ["/usr/sbin/screencapture", "-x", str(target_file)]
    ok, out = run_screencapture(cmd, target_file, "all-displays")
    if ok:
        return True, out
    if out:
        last_error = out
    return False, last_error


def control_focus(app: str) -> tuple[bool, str]:
    if app not in ALLOWED_APPS:
        return False, f"app '{app}' is not in allowlist"
    return run_applescript([f'tell application "{app}" to activate'], timeout_seconds=2.0)


def control_open(app: str = "", url: str = "", path_value: str = "") -> tuple[bool, str]:
    app = app.strip()
    url = url.strip()
    path_value = path_value.strip()
    provided = int(bool(app)) + int(bool(url)) + int(bool(path_value))
    if provided != 1:
        return False, "provide exactly one of app, url, or path"

    if app:
        if app not in ALLOWED_APPS:
            return False, f"app '{app}' is not in allowlist"
        return run_process(["/usr/bin/open", "-a", app], timeout_seconds=3.0)

    if url:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES or not parsed.netloc:
            return False, "only http/https URLs are allowed"
        return run_process(["/usr/bin/open", url], timeout_seconds=3.0)

    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        return False, "path must be absolute"
    resolved = candidate.resolve(strict=False)
    if not resolved.exists():
        return False, f"path '{resolved}' does not exist"
    if not any(path_within_root(resolved, root) for root in allowed_open_roots()):
        return False, f"path '{resolved}' is outside allowed roots"
    return run_process(["/usr/bin/open", str(resolved)], timeout_seconds=3.0)


def control_type(text: str) -> tuple[bool, str]:
    return run_inputctl(["type", text], timeout_seconds=3.0)


def control_key(key_name: str) -> tuple[bool, str]:
    key = key_name.strip().lower()
    if key not in KEY_CODES:
        return False, f"unsupported key '{key}'"
    return run_inputctl(["key", key], timeout_seconds=2.0)


def control_click(x: int, y: int) -> tuple[bool, str]:
    return run_inputctl(["click", str(x), str(y)], timeout_seconds=2.0)


def control_shortcut(key_name: str, modifiers: list[str]) -> tuple[bool, str]:
    key = key_name.strip().lower()
    if not key:
        return False, "missing key"
    cleaned_mods: list[str] = []
    for raw in modifiers:
        mod = raw.strip().lower()
        if not mod:
            continue
        if mod not in ALLOWED_MODIFIERS:
            return False, f"unsupported modifier '{mod}'"
        cleaned_mods.append(mod)
    return run_inputctl(["shortcut", key, *cleaned_mods], timeout_seconds=2.0)


class Handler(BaseHTTPRequestHandler):
    frame_lock = threading.Lock()
    frame_file = Path(tempfile.gettempdir()) / "virtualos_frame.jpg"
    cached_frame_key = "none"
    cached_frame_data: bytes | None = None
    cached_frame_mode = "none"
    cached_frame_at = 0.0
    frame_cache_ttl_seconds = 0.25

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-VOS-Token, X-Remote-Token",
        )
        self.send_header("Access-Control-Max-Age", "86400")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    @classmethod
    def get_latest_frame(
        cls,
        source: str = "window",
        capture_app: str = "virtualOS",
        max_dim: int = 0,
    ) -> tuple[bool, str, bytes]:
        with cls.frame_lock:
            now = time.monotonic()
            frame_key = f"{source}:{capture_app.lower()}:max{max_dim}"
            if (
                cls.cached_frame_key == frame_key
                and cls.cached_frame_data
                and (now - cls.cached_frame_at) <= cls.frame_cache_ttl_seconds
            ):
                return True, f"{cls.cached_frame_mode};cache=hit", cls.cached_frame_data

            ok, mode = capture_frame(cls.frame_file, source=source, capture_app=capture_app)
            if ok:
                data_ok, data, data_note = read_frame_bytes(cls.frame_file, max_dim=max_dim)
                if not data_ok:
                    if cls.cached_frame_data and cls.cached_frame_key == frame_key:
                        age = now - cls.cached_frame_at
                        return True, f"{cls.cached_frame_mode};cache=stale-fallback;age={age:.2f}s", cls.cached_frame_data
                    return False, data_note, b""
                cls.cached_frame_key = frame_key
                cls.cached_frame_data = data
                cls.cached_frame_mode = mode + data_note
                cls.cached_frame_at = now
                return True, f"{cls.cached_frame_mode};cache=miss", data

            if cls.cached_frame_data and cls.cached_frame_key == frame_key:
                age = now - cls.cached_frame_at
                return True, f"{cls.cached_frame_mode};cache=stale-fallback;age={age:.2f}s", cls.cached_frame_data
            return False, mode, b""

    def _write_json_obj(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write_html(self, code: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _authorized(self) -> bool:
        if not REQUIRED_AUTH_TOKEN:
            return True

        candidates: list[str] = []

        auth = self.headers.get("Authorization", "")
        if auth:
            lower = auth.lower()
            if lower.startswith("bearer "):
                candidates.append(auth[7:].strip())
            else:
                candidates.append(auth.strip())

        for header in ("X-VOS-Token", "X-Remote-Token"):
            value = self.headers.get(header, "").strip()
            if value:
                candidates.append(value)

        return any(token == REQUIRED_AUTH_TOKEN for token in candidates)

    def _reject_unauthorized(self) -> None:
        self._write_json_obj(401, {"ok": False, "error": "unauthorized"})

    def _debug_page(self) -> str:
        return """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>virtualOS Stream + Control Debug</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; margin: 16px; background: #111; color: #eee; }
    .row { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
    button, input, textarea { font-size: 14px; padding: 8px 10px; border-radius: 8px; border: 1px solid #333; background: #1d1d1d; color: #fff; }
    textarea { width: min(900px, 100%); height: 70px; }
    #status { color: #9ad; }
    #controlStatus { color: #fc9; white-space: pre-wrap; }
    #systemStatus { color: #a9f1bd; white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    #frame { display: block; width: min(1200px, 100%); border: 1px solid #333; border-radius: 10px; background: #000; }
    code { color: #9f9; }
    h3 { margin-top: 20px; }
  </style>
</head>
<body>
  <h2>virtualOS Stream + Control Debug</h2>

  <h3>Stream</h3>
  <div class=\"row\">
    <label>App</label>
    <input id=\"captureApp\" value=\"Codex\" />
    <label>Source</label>
    <select id=\"captureSource\">
      <option value=\"window\">window</option>
      <option value=\"screen\">screen</option>
    </select>
    <label>Interval (ms)</label>
    <input id=\"interval\" type=\"number\" value=\"1000\" min=\"500\" step=\"100\" />
    <button id=\"toggle\">Start</button>
    <button id=\"single\">Single Frame</button>
    <span id=\"status\">idle</span>
  </div>
  <div class=\"row\">Endpoint: <code>/frame.jpg</code></div>
  <img id=\"frame\" alt=\"frame\" />

  <h3>Control (safe allowlist)</h3>
  <div class=\"row\">
    <input id=\"appName\" value=\"Codex\" placeholder=\"App name\" />
    <button id=\"focusBtn\">Focus App</button>
  </div>
  <div class=\"row\">
    <textarea id=\"typeText\" placeholder=\"Type into focused input...\"></textarea>
  </div>
  <div class=\"row\">
    <button id=\"typeBtn\">Type Text</button>
    <button id=\"typeEnterBtn\">Type + Enter</button>
    <button id=\"enterBtn\">Press Enter</button>
  </div>
  <div class=\"row\">
    <input id=\"clickX\" value=\"500\" style=\"width:90px\" />
    <input id=\"clickY\" value=\"400\" style=\"width:90px\" />
    <button id=\"clickBtn\">Click XY</button>
  </div>
  <div class=\"row\">
    <input id=\"openTarget\" placeholder=\"App name, https:// URL, or /absolute/path\" style=\"width:min(900px,100%)\" />
    <button id=\"openBtn\">Open</button>
  </div>
  <div id=\"controlStatus\">idle</div>

  <h3>System</h3>
  <div class=\"row\">
    <button id=\"refreshSystemBtn\">Refresh Repo Status</button>
  </div>
  <div id=\"systemStatus\">loading…</div>

  <script>
    const frame = document.getElementById('frame');
    const statusEl = document.getElementById('status');
    const toggle = document.getElementById('toggle');
    const single = document.getElementById('single');
    const intervalInput = document.getElementById('interval');
    const captureApp = document.getElementById('captureApp');
    const captureSource = document.getElementById('captureSource');
    const controlStatus = document.getElementById('controlStatus');
    const systemStatus = document.getElementById('systemStatus');
    let timer = null;
    let running = false;
    let pending = false;

    function scheduleNext() {
      if (!running || document.visibilityState !== 'visible') return;
      const ms = Math.max(500, Number(intervalInput.value || 1000));
      timer = setTimeout(pullOnce, ms);
    }

    function pullOnce() {
      if (pending || document.visibilityState !== 'visible') return;
      pending = true;
      const ts = Date.now();
      const app = encodeURIComponent((captureApp.value || '').trim());
      const source = encodeURIComponent(captureSource.value || 'window');
      frame.src = `/frame.jpg?ts=${ts}&app=${app}&source=${source}`;
      statusEl.textContent = `requested ${new Date().toLocaleTimeString()}`;
    }

    frame.onload = () => { statusEl.textContent = `ok ${new Date().toLocaleTimeString()}`; };
    frame.onerror = () => { statusEl.textContent = `error ${new Date().toLocaleTimeString()}`; };
    frame.addEventListener('load', () => { pending = false; scheduleNext(); });
    frame.addEventListener('error', () => { pending = false; scheduleNext(); });
    single.onclick = pullOnce;

    toggle.onclick = () => {
      if (running) {
        running = false;
        clearTimeout(timer);
        timer = null;
        toggle.textContent = 'Start';
        statusEl.textContent = 'stopped';
        return;
      }
      const ms = Math.max(500, Number(intervalInput.value || 1000));
      running = true;
      pullOnce();
      toggle.textContent = 'Stop';
      statusEl.textContent = `running every ${ms}ms`;
    };
    document.addEventListener('visibilitychange', () => {
      clearTimeout(timer);
      timer = null;
      if (document.visibilityState === 'visible' && running && !pending) pullOnce();
      else if (running) statusEl.textContent = 'paused while hidden';
    });

    async function post(path, payload) {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
      });
      const data = await res.json();
      controlStatus.textContent = `${path}: ${JSON.stringify(data)}`;
    }

    document.getElementById('focusBtn').onclick = async () => {
      await post('/control/focus', { app: document.getElementById('appName').value.trim() });
    };

    document.getElementById('typeBtn').onclick = async () => {
      await post('/control/type', { text: document.getElementById('typeText').value });
    };

    document.getElementById('typeEnterBtn').onclick = async () => {
      const text = document.getElementById('typeText').value;
      await post('/control/type', { text });
      await post('/control/key', { key: 'enter' });
    };

    document.getElementById('enterBtn').onclick = async () => {
      await post('/control/key', { key: 'enter' });
    };

    document.getElementById('clickBtn').onclick = async () => {
      await post('/control/click', {
        x: Number(document.getElementById('clickX').value),
        y: Number(document.getElementById('clickY').value)
      });
    };

    document.getElementById('openBtn').onclick = async () => {
      const value = (document.getElementById('openTarget').value || '').trim();
      if (!value) {
        controlStatus.textContent = 'open target is empty';
        return;
      }
      const payload = /^https?:\\/\\//i.test(value)
        ? { url: value }
        : (value.startsWith('/') || value.startsWith('~'))
          ? { path: value }
          : { app: value };
      await post('/control/open', payload);
    };

    async function refreshSystemStatus() {
      try {
        const res = await fetch('/api/system/status?ts=' + Date.now());
        const data = await res.json();
        const repos = Array.isArray(data.repos) ? data.repos : [];
        if (!repos.length) {
          systemStatus.textContent = 'No watched repos configured.';
          return;
        }
        systemStatus.textContent = repos.map((repo) => {
          if (!repo.exists) return `${repo.name}: missing`;
          if (!repo.ok) return `${repo.name}: ${repo.error || 'error'}`;
          const state = repo.behind > 0
            ? `behind ${repo.behind}`
            : repo.dirty
              ? 'dirty'
              : 'up-to-date';
          return `${repo.name}: ${state} @ ${repo.headShort || '-'} (${repo.branch || 'detached'})`;
        }).join('\\n');
      } catch (error) {
        systemStatus.textContent = `repo status failed: ${error}`;
      }
    }

    document.getElementById('refreshSystemBtn').onclick = refreshSystemStatus;
    refreshSystemStatus();
    setInterval(refreshSystemStatus, 15000);
  </script>
</body>
</html>
"""

    def _vm_logs_page(self) -> str:
        return """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>virtualOS VM Logs</title>
  <style>
    body { background:#0f1115; color:#e9eef5; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 14px; }
    .grid { display:grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap:10px; margin-bottom:12px; }
    .card { border:1px solid #2b3340; background:#171c24; border-radius:10px; padding:10px; }
    .muted { color:#a4afbf; font-size:12px; }
    .big { font-size:22px; font-weight:700; }
    .warn { color:#ffcc66; }
    .bad { color:#ff6b6b; }
    .ok { color:#7dd97d; }
    pre { border:1px solid #2b3340; border-radius:10px; background:#0b0d12; padding:12px; height:58vh; overflow:auto; white-space:pre-wrap; line-height:1.3; }
    button { background:#243044; color:#fff; border:1px solid #364866; border-radius:8px; padding:8px 12px; cursor:pointer; }
    .row { display:flex; gap:10px; align-items:center; margin-bottom:10px; flex-wrap:wrap; }
  </style>
</head>
<body>
  <h2>virtualOS VM Logs</h2>
  <div class=\"row\">
    <button id=\"pauseBtn\">Pause</button>
    <button id=\"clearBtn\">Clear View</button>
    <span id=\"monitor\" class=\"muted\">monitor: --</span>
    <span id=\"lastUpdated\" class=\"muted\">--</span>
  </div>

  <div class=\"grid\">
    <div class=\"card\">
      <div class=\"muted\">Host Data Volume Used</div>
      <div id=\"usedPct\" class=\"big\">--</div>
    </div>
    <div class=\"card\">
      <div class=\"muted\">Host Free (GB)</div>
      <div id=\"freeGB\" class=\"big\">--</div>
    </div>
    <div class=\"card\">
      <div class=\"muted\">VM Disk Allocated (GB)</div>
      <div id=\"vmAllocGB\" class=\"big\">--</div>
    </div>
    <div class=\"card\">
      <div class=\"muted\">VM Config (CPU / RAM)</div>
      <div id=\"vmConfig\" class=\"big\">--</div>
    </div>
  </div>

  <div class=\"card\" style=\"margin-bottom:10px;\">
    <div class=\"muted\">Recent failure lines</div>
    <pre id=\"failures\" style=\"height:120px; margin-top:8px;\"></pre>
  </div>

  <pre id=\"logs\"></pre>

  <script>
    let since = 0;
    let paused = false;
    const logsEl = document.getElementById('logs');
    const failuresEl = document.getElementById('failures');
    const pauseBtn = document.getElementById('pauseBtn');
    const clearBtn = document.getElementById('clearBtn');
    const monitorEl = document.getElementById('monitor');
    const lastUpdatedEl = document.getElementById('lastUpdated');

    function appendLines(lines) {
      if (!lines || !lines.length) return;
      const atBottom = Math.abs(logsEl.scrollHeight - logsEl.scrollTop - logsEl.clientHeight) < 10;
      for (const item of lines) {
        logsEl.textContent += item.line + "\\n";
      }
      if (logsEl.textContent.length > 200000) {
        logsEl.textContent = logsEl.textContent.slice(-180000);
      }
      if (atBottom) {
        logsEl.scrollTop = logsEl.scrollHeight;
      }
    }

    async function refreshStatus() {
      const res = await fetch('/api/vm/status?ts=' + Date.now());
      if (!res.ok) return;
      const s = await res.json();
      const usedPct = s?.hostDataVolume?.usedPct ?? 0;
      const freeGB = s?.hostDataVolume?.freeGB ?? 0;
      const alloc = s?.vmBundle?.diskAllocatedGB ?? 0;
      const params = s?.vmBundle?.params || {};
      document.getElementById('usedPct').textContent = usedPct + '%';
      document.getElementById('freeGB').textContent = freeGB;
      document.getElementById('vmAllocGB').textContent = alloc;
      document.getElementById('vmConfig').textContent = `${params.cpuCount ?? '-'} / ${params.memorySizeInGB ?? '-'}GB`;
      monitorEl.textContent = `monitor: ${s.monitorStatus || 'unknown'}`;
      lastUpdatedEl.textContent = `updated ${new Date().toLocaleTimeString()}`;
      document.getElementById('usedPct').className = 'big ' + (usedPct >= 95 ? 'bad' : usedPct >= 90 ? 'warn' : 'ok');
      document.getElementById('freeGB').className = 'big ' + (freeGB <= 15 ? 'bad' : freeGB <= 30 ? 'warn' : 'ok');
      failuresEl.textContent = (s.recentFailureLines || []).join('\\n') || '(none)';
    }

    async function refreshLogs() {
      if (paused) return;
      const res = await fetch(`/api/vm/logs?since=${since}&limit=500&ts=${Date.now()}`);
      if (!res.ok) return;
      const payload = await res.json();
      since = payload.lastSeq || since;
      appendLines(payload.logs || []);
    }

    pauseBtn.onclick = () => {
      paused = !paused;
      pauseBtn.textContent = paused ? 'Resume' : 'Pause';
    };
    clearBtn.onclick = () => {
      logsEl.textContent = '';
    };

    refreshStatus();
    refreshLogs();
    setInterval(refreshStatus, 5000);
    setInterval(refreshLogs, 1200);
  </script>
</body>
</html>
"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._write_json_obj(
                200,
                {
                    "ok": True,
                    "endpoints": [
                        "/health",
                        "/discover",
                        "/debug",
                        "/vm/logs",
                        "/debug/state",
                        "/api/vm/status",
                        "/api/vm/logs",
                        "/api/system/status",
                        "/api/system/pull",
                        "/frame.jpg",
                        "/control/open",
                        "/control/focus",
                        "/control/type",
                        "/control/key",
                        "/control/shortcut",
                        "/control/click",
                    ],
                    "allowedApps": sorted(ALLOWED_APPS),
                    "bonjourServiceType": "_virtualosremote._tcp",
                    "authRequired": bool(REQUIRED_AUTH_TOKEN),
                },
            )
            return

        if path == "/discover":
            self._write_json_obj(
                200,
                {
                    "ok": True,
                    "hostName": socket.gethostname(),
                    "port": self.server.server_port if hasattr(self.server, "server_port") else None,
                    "localIPv4": get_local_ipv4_addresses(),
                    "bonjourServiceType": "_virtualosremote._tcp",
                    "authRequired": bool(REQUIRED_AUTH_TOKEN),
                },
            )
            return

        if not self._authorized():
            self._reject_unauthorized()
            return

        if path in ["/", "/debug"]:
            self._write_html(200, self._debug_page())
            return

        if path == "/vm/logs":
            self._write_html(200, self._vm_logs_page())
            return

        if path == "/debug/state":
            app = str(query.get("app", ["virtualOS"])[0] or "virtualOS").strip() or "virtualOS"
            payload = {
                "ok": True,
                "hostName": socket.gethostname(),
                "listenPort": self.server.server_port if hasattr(self.server, "server_port") else None,
                "displayUnion": get_display_union_bounds(),
                "displayRects": get_display_rects(),
                "displayIds": get_display_ids(),
                "mainDisplayId": get_main_display_id(),
                "targetApp": app,
                "targetAppRect": get_app_window_rect(app),
                "time": int(time.time()),
            }
            self._write_json_obj(200, payload)
            return

        if path == "/api/vm/status":
            self._write_json_obj(200, VM_LOG_MONITOR.status())
            return

        if path == "/api/vm/logs":
            try:
                since_seq = int(str(query.get("since", ["0"])[0] or "0"))
            except ValueError:
                since_seq = 0
            try:
                limit = int(str(query.get("limit", ["400"])[0] or "400"))
            except ValueError:
                limit = 400
            limit = clamp_int(limit, 50, 1000)
            last_seq, logs = VM_LOG_MONITOR.snapshot(since_seq=since_seq, limit=limit)
            self._write_json_obj(200, {"ok": True, "lastSeq": last_seq, "logs": logs})
            return

        if path == "/api/system/status":
            self._write_json_obj(200, REPO_STATUS_MONITOR.status())
            return

        if path != "/frame.jpg":
            self._write_json_obj(404, {"ok": False, "error": "not found"})
            return

        source_raw = query.get("source", [""])[0].strip().lower()
        full_flag = query.get("full", [""])[0] in {"1", "true", "yes"}
        if full_flag:
            source = "all"
        elif source_raw in {"window"}:
            source = "window"
        elif source_raw in {"screen", "fullscreen"}:
            source = "screen"
        elif source_raw in {"all", "desktop"}:
            source = "all"
        else:
            source = "window"
        capture_app = str(query.get("app", ["virtualOS"])[0] or "virtualOS").strip() or "virtualOS"
        max_dim_raw = str(query.get("max", [""])[0] or "").strip()
        max_dim = 0
        if max_dim_raw:
            try:
                parsed = int(max_dim_raw)
                if parsed > 0:
                    max_dim = clamp_int(parsed, 320, 4096)
            except ValueError:
                max_dim = 0

        ok, mode, data = self.get_latest_frame(source=source, capture_app=capture_app, max_dim=max_dim)
        if not ok:
            self._write_json_obj(500, {"ok": False, "error": mode})
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("X-Capture-Mode", mode)
        self.send_header("X-Frame-Max", str(max_dim))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self._read_json_body()

        if not self._authorized():
            self._reject_unauthorized()
            return

        if path == "/control/focus":
            app = str(payload.get("app", "")).strip()
            if not app:
                self._write_json_obj(400, {"ok": False, "error": "missing app"})
                return
            ok, out = control_focus(app)
            self._write_json_obj(200 if ok else 400, {"ok": ok, "result": out})
            return

        if path == "/control/open":
            app = str(payload.get("app", "")).strip()
            url = str(payload.get("url", "")).strip()
            path_value = str(payload.get("path", "")).strip()
            ok, out = control_open(app=app, url=url, path_value=path_value)
            self._write_json_obj(200 if ok else 400, {"ok": ok, "result": out})
            return

        if path == "/api/system/pull":
            repo_ref = str(payload.get("repo", "")).strip()
            ok, result = REPO_STATUS_MONITOR.pull_repo(repo_ref)
            self._write_json_obj(200 if ok else 400, result)
            return

        if path == "/control/type":
            text = str(payload.get("text", ""))
            ok, out = control_type(text)
            self._write_json_obj(200 if ok else 400, {"ok": ok, "result": out})
            return

        if path == "/control/key":
            key = str(payload.get("key", "")).strip().lower()
            if not key:
                self._write_json_obj(400, {"ok": False, "error": "missing key"})
                return
            ok, out = control_key(key)
            self._write_json_obj(200 if ok else 400, {"ok": ok, "result": out})
            return

        if path == "/control/shortcut":
            key = str(payload.get("key", "")).strip().lower()
            if not key:
                self._write_json_obj(400, {"ok": False, "error": "missing key"})
                return
            mods = payload.get("modifiers", [])
            if mods is None:
                mods = []
            if not isinstance(mods, list):
                self._write_json_obj(400, {"ok": False, "error": "modifiers must be an array"})
                return
            modifiers = [str(m) for m in mods]
            ok, out = control_shortcut(key, modifiers)
            self._write_json_obj(200 if ok else 400, {"ok": ok, "result": out})
            return

        if path == "/control/click":
            if "nx" in payload or "ny" in payload:
                try:
                    nx = float(payload.get("nx"))
                    ny = float(payload.get("ny"))
                except Exception:
                    self._write_json_obj(400, {"ok": False, "error": "nx and ny must be numbers"})
                    return
                source = str(payload.get("source", "window")).strip().lower()
                target_app = str(payload.get("app", "virtualOS")).strip() or "virtualOS"
                mapped_ok, mapped_point, mapped_detail = map_normalized_click_to_quartz(nx, ny, source, target_app)
                if not mapped_ok or not mapped_point:
                    self._write_json_obj(400, {"ok": False, "error": mapped_detail})
                    return
                x, y = mapped_point
                ok, out = control_click(x, y)
                self._write_json_obj(
                    200 if ok else 400,
                    {
                        "ok": ok,
                        "result": out,
                        "mapped": {"x": x, "y": y, "source": source, "app": target_app, "target": mapped_detail},
                    },
                )
                return

            try:
                x = int(payload.get("x"))
                y = int(payload.get("y"))
            except Exception:
                self._write_json_obj(400, {"ok": False, "error": "x and y must be integers"})
                return
            ok, out = control_click(x, y)
            self._write_json_obj(200 if ok else 400, {"ok": ok, "result": out})
            return

        self._write_json_obj(404, {"ok": False, "error": "not found"})

    def log_message(self, format: str, *args):
        return


def main():
    global REQUIRED_AUTH_TOKEN
    parser = argparse.ArgumentParser(description="Simple virtualOS stream + control server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8899, type=int)
    parser.add_argument("--no-bonjour", action="store_true", help="disable Bonjour advertisement")
    parser.add_argument("--auth-token", default=os.getenv("VOS_AUTH_TOKEN", ""), help="require this token for non-health endpoints")
    args = parser.parse_args()
    REQUIRED_AUTH_TOKEN = args.auth_token.strip()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not REQUIRED_AUTH_TOKEN:
        parser.error("a VOS_AUTH_TOKEN or --auth-token is required for a network-wide bind")

    VM_LOG_MONITOR.start()
    atexit.register(VM_LOG_MONITOR.stop)
    REPO_STATUS_MONITOR.start()
    atexit.register(REPO_STATUS_MONITOR.stop)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    advertiser: BonjourAdvertiser | None = None
    if not args.no_bonjour and args.host not in ("127.0.0.1", "localhost", "::1"):
        advertiser = BonjourAdvertiser(args.port)
        ok, msg = advertiser.start()
        if ok:
            print(f"Bonjour: {msg}")
            atexit.register(advertiser.stop)
        else:
            print(f"Bonjour: {msg}")

    print(f"Serving on http://{args.host}:{args.port}")
    print("Endpoints: /health, /debug, /vm/logs, /api/vm/*, /api/system/*, /frame.jpg, /control/*")
    if REQUIRED_AUTH_TOKEN:
        print("Auth: enabled (Bearer token required for /debug, /frame.jpg, /control/*)")
    else:
        print("Auth: disabled")
    print("Tip: Keep virtualOS visible. For control, grant Accessibility + Screen Recording permissions.")
    try:
        server.serve_forever()
    finally:
        if advertiser:
            advertiser.stop()


if __name__ == "__main__":
    main()
