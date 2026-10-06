#!/usr/bin/env python3
"""
ai_watchdog.py — AI-powered self-healing monitor for virtualOS

Uses Claude API to analyze system state and decide corrective actions.
Connects the host monitoring to the AI agent running inside the VM.

Usage:
    ANTHROPIC_API_KEY=sk-... VOS_VM_PASSWORD=xxx python3 tools/ai_watchdog.py

Optional:
    RELAY_URL=... AUTH_TOKEN=... (for Cloudflare relay heartbeat)
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
STREAM_SERVER = os.environ.get("STREAM_SERVER", "http://127.0.0.1:8899")
LOG_FILE = REPO_DIR / "tools" / "ai_watchdog.log"
STATE_FILE = Path("/tmp/vos_ai_watchdog_state.json")

# Adaptive timing
CHECK_INTERVAL = 20
MIN_INTERVAL = 10
MAX_INTERVAL = 120
consecutive_ok = 0
consecutive_fail = 0
total_fixes = 0
total_checks = 0
ai_consultations = 0

# Rate limits
MAX_AI_CALLS_PER_HOUR = 20
ai_calls_this_hour = 0
hour_start = time.time()

# History for AI context
event_log: list[dict] = []
MAX_EVENT_LOG = 100


def log(msg: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    colors = {"info": "\033[0;90m", "ok": "\033[0;32m", "warn": "\033[0;33m",
              "fix": "\033[0;36m", "err": "\033[0;31m", "ai": "\033[0;35m"}
    color = colors.get(level, "\033[0m")
    line = f"[{ts}] {level.upper()}: {msg}"
    print(f"{color}{line}\033[0m")
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    event_log.append({"ts": time.time(), "level": level, "msg": msg})
    if len(event_log) > MAX_EVENT_LOG:
        del event_log[:len(event_log) - MAX_EVENT_LOG]


# ── system checks ────────────────────────────────────────────────────

def check_app_running() -> bool:
    return subprocess.run(["pgrep", "-x", "virtualOS"],
                          capture_output=True).returncode == 0


def check_vm_window_count() -> int:
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to tell process "virtualOS" to count of windows'],
            capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip()) if r.returncode == 0 else 0
    except Exception:
        return 0


def check_stream_server() -> dict | None:
    try:
        r = subprocess.run(
            ["curl", "-sf", "--connect-timeout", "1", "--max-time", "3",
             f"{STREAM_SERVER}/api/vm/status"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def check_vm_ping() -> tuple[str, bool]:
    try:
        r = subprocess.run(
            ["arp", "-a"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            if "bridge100" in line:
                ip = line.split("(")[1].split(")")[0] if "(" in line else ""
                if ip:
                    ping = subprocess.run(
                        ["ping", "-c", "1", "-W", "1", ip],
                        capture_output=True, timeout=3)
                    return ip, ping.returncode == 0
    except Exception:
        pass
    return "", False


def check_vm_ports(ip: str) -> list[int]:
    open_ports = []
    if not ip:
        return open_ports
    for port in [22, 80, 443, 3000, 5000, 5900, 8080]:
        try:
            r = subprocess.run(
                ["nc", "-z", "-w", "1", ip, str(port)],
                capture_output=True, timeout=3)
            if r.returncode == 0:
                open_ports.append(port)
        except Exception:
            pass
    return open_ports


def get_disk_free() -> int:
    try:
        r = subprocess.run(["df", "-g", "/"], capture_output=True, text=True, timeout=3)
        return int(r.stdout.strip().split("\n")[-1].split()[3])
    except Exception:
        return 0


def get_cpu_load() -> float:
    try:
        r = subprocess.run(["sysctl", "-n", "vm.loadavg"],
                           capture_output=True, text=True, timeout=3)
        return float(r.stdout.strip().split()[1])
    except Exception:
        return 0.0


def capture_vm_screenshot() -> str | None:
    """Capture a screenshot and return the path."""
    path = "/tmp/vos_watchdog_screen.jpg"
    try:
        r = subprocess.run(
            ["curl", "-sf", "--connect-timeout", "2", "--max-time", "5",
             "-o", path,
             f"{STREAM_SERVER}/frame.jpg?source=window&app=virtualOS&max=800"],
            capture_output=True, timeout=8)
        if r.returncode == 0 and os.path.exists(path):
            return path
    except Exception:
        pass
    return None


def collect_system_state() -> dict:
    """Gather all system metrics into one dict."""
    app_running = check_app_running()
    window_count = check_vm_window_count() if app_running else 0
    vm_running = app_running and window_count > 1
    stream_status = check_stream_server()
    vm_ip, vm_ping = check_vm_ping()
    vm_ports = check_vm_ports(vm_ip) if vm_ping else []
    disk_free = get_disk_free()
    cpu_load = get_cpu_load()

    state = {
        "timestamp": datetime.now().isoformat(),
        "app_running": app_running,
        "vm_running": vm_running,
        "window_count": window_count,
        "stream_server": stream_status is not None,
        "vm_ip": vm_ip,
        "vm_ping": vm_ping,
        "vm_open_ports": vm_ports,
        "ssh_available": 22 in vm_ports,
        "disk_free_gb": disk_free,
        "cpu_load": cpu_load,
        "consecutive_ok": consecutive_ok,
        "consecutive_fail": consecutive_fail,
        "total_fixes": total_fixes,
        "total_checks": total_checks,
        "check_interval": CHECK_INTERVAL,
    }

    if stream_status:
        bundle = stream_status.get("vmBundle", {})
        params = bundle.get("params", {})
        host_vol = stream_status.get("hostDataVolume", {})
        state["vm_version"] = params.get("version", "")
        state["vm_cpu"] = params.get("cpuCount", 0)
        state["vm_memory_gb"] = params.get("memorySizeInGB", 0)
        state["vm_disk_allocated_gb"] = bundle.get("diskAllocatedGB", 0)
        state["vm_disk_logical_gb"] = bundle.get("diskLogicalGB", 0)
        state["monitor_status"] = stream_status.get("monitorStatus", "unknown")
        state["recent_failures"] = len(stream_status.get("recentFailureLines", []))
        state["host_disk_free_gb"] = host_vol.get("freeGB", 0)

    # Recent event log
    state["recent_events"] = event_log[-15:]

    return state


# ── actions ──────────────────────────────────────────────────────────

def action_start_app():
    log("Starting virtualOS app", "fix")
    cmd = [str(REPO_DIR / "tools" / "vosctl"), "start"]
    subprocess.run(cmd, capture_output=True, timeout=120)
    time.sleep(5)


def action_start_vm():
    log("Clicking Start button", "fix")
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to tell process "virtualOS" to click button "Start" of window "virtualOS"'],
        capture_output=True, timeout=10)
    time.sleep(5)
    # The app checks the guest login prompt and unlocks it when configured.


def action_start_stream():
    log("Starting stream server", "fix")
    subprocess.Popen(
        ["python3", str(REPO_DIR / "tools" / "virtualos_stream_server" / "server.py"),
         "--port", "8899"],
        stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT)
    time.sleep(2)


def action_restart_vm():
    log("Restarting VM (force)", "fix")
    subprocess.run(["osascript", "-e", 'tell application "virtualOS" to quit'],
                   capture_output=True, timeout=10)
    time.sleep(3)
    if check_app_running():
        subprocess.run(["pkill", "-9", "virtualOS"], capture_output=True)
        time.sleep(2)
    action_start_app()


def action_clear_caches():
    log("Clearing caches for disk space", "fix")
    for d in ["~/Library/Developer/Xcode/DerivedData", str(REPO_DIR / "build")]:
        subprocess.run(["rm", "-rf", os.path.expanduser(d)], capture_output=True)


def action_send_to_vm_clipboard(text: str):
    """Send text to VM via clipboard sharing."""
    log(f"Sending to VM clipboard: {text[:60]}...", "fix")
    subprocess.run(["pbcopy"], input=text.encode(), capture_output=True)
    time.sleep(0.3)
    # Focus VM and paste
    subprocess.run(
        ["osascript", "-e", """
tell application "System Events"
    tell process "virtualOS"
        set frontmost to true
        repeat with w in every window
            if name of w is not "virtualOS" then
                perform action "AXRaise" of w
                exit repeat
            end if
        end repeat
    end tell
    delay 0.3
    click at {457, 613}
    delay 0.3
    keystroke "v" using command down
    delay 0.2
    key code 36
end tell"""],
        capture_output=True, timeout=10)


AVAILABLE_ACTIONS = {
    "start_app": action_start_app,
    "start_vm": action_start_vm,
    "start_stream": action_start_stream,
    "restart_vm": action_restart_vm,
    "clear_caches": action_clear_caches,
    "none": lambda: None,
}


# ── AI diagnosis ─────────────────────────────────────────────────────

def ask_ai(system_state: dict) -> list[str]:
    """Ask Claude to analyze system state and recommend actions."""
    global ai_calls_this_hour, ai_consultations

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return decide_without_ai(system_state)

    if ai_calls_this_hour >= MAX_AI_CALLS_PER_HOUR:
        log("AI call limit reached, using rule-based fallback", "warn")
        return decide_without_ai(system_state)

    ai_calls_this_hour += 1
    ai_consultations += 1
    log("Consulting AI for diagnosis...", "ai")

    prompt = f"""You are the watchdog for a virtualOS system — a macOS VM running AI tools (Codex).
Analyze this system state and respond with a JSON array of action names to take.

Available actions: {list(AVAILABLE_ACTIONS.keys())}
Also available: "send_clipboard:<command>" to run a command inside the VM via clipboard paste.

System state:
{json.dumps(system_state, indent=2, default=str)}

Rules:
- Return ["none"] if everything is healthy
- Only return actions that fix actual problems
- Don't restart things that are already running
- If SSH is not available and you need to run commands in the VM, use "send_clipboard:command here"
- If the VM is running but you see consecutive failures, consider restarting
- Be conservative — prefer "none" over unnecessary restarts
- Respond with ONLY a JSON array, no explanation"""

    try:
        import urllib.request
        body = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            text = data["content"][0]["text"].strip()
            # Parse JSON array from response
            if text.startswith("["):
                actions = json.loads(text)
                log(f"AI recommends: {actions}", "ai")
                return actions
            else:
                log(f"AI response not JSON: {text[:100]}", "warn")
                return decide_without_ai(system_state)
    except Exception as e:
        log(f"AI call failed: {e}", "err")
        return decide_without_ai(system_state)


def decide_without_ai(state: dict) -> list[str]:
    """Rule-based fallback when AI is unavailable."""
    actions = []
    if not state["app_running"]:
        actions.append("start_app")
    elif not state["vm_running"]:
        actions.append("start_vm")
    if not state["stream_server"] and state["app_running"]:
        actions.append("start_stream")
    if state["disk_free_gb"] < 10:
        actions.append("clear_caches")
    if state.get("consecutive_fail", 0) > 5 and state["vm_running"]:
        actions.append("restart_vm")
    if not actions:
        actions.append("none")
    return actions


# ── execute actions ──────────────────────────────────────────────────

def execute_actions(actions: list[str]):
    global total_fixes
    for action in actions:
        if action == "none":
            continue
        if action.startswith("send_clipboard:"):
            cmd = action[len("send_clipboard:"):]
            action_send_to_vm_clipboard(cmd)
            total_fixes += 1
        elif action in AVAILABLE_ACTIONS:
            AVAILABLE_ACTIONS[action]()
            total_fixes += 1
        else:
            log(f"Unknown action: {action}", "warn")


# ── main loop ────────────────────────────────────────────────────────

def adapt_interval():
    global CHECK_INTERVAL
    if consecutive_fail > 0:
        CHECK_INTERVAL = max(MIN_INTERVAL, CHECK_INTERVAL - 2)
    elif consecutive_ok > 10:
        CHECK_INTERVAL = min(MAX_INTERVAL, CHECK_INTERVAL + 5)
    elif consecutive_ok > 3:
        CHECK_INTERVAL = min(60, CHECK_INTERVAL + 1)


def run_check():
    global consecutive_ok, consecutive_fail, total_checks
    global ai_calls_this_hour, hour_start
    total_checks += 1

    # Reset hourly counters
    if time.time() - hour_start >= 3600:
        hour_start = time.time()
        ai_calls_this_hour = 0

    state = collect_system_state()

    # Determine if we need AI or simple rules are enough
    has_problems = (
        not state["app_running"]
        or not state["vm_running"]
        or not state["stream_server"]
        or state["disk_free_gb"] < 10
        or state.get("recent_failures", 0) > 0
        or consecutive_fail > 2
    )

    if has_problems:
        actions = ask_ai(state)
        execute_actions(actions)
        consecutive_fail += 1
        consecutive_ok = 0
    else:
        consecutive_ok += 1
        consecutive_fail = 0
        if consecutive_ok % 20 == 0 and consecutive_ok > 0:
            log(f"Healthy ({consecutive_ok} checks, interval: {CHECK_INTERVAL}s)", "ok")

    adapt_interval()

    # Save state
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "check_interval": CHECK_INTERVAL,
                "consecutive_ok": consecutive_ok,
                "consecutive_fail": consecutive_fail,
                "total_fixes": total_fixes,
                "total_checks": total_checks,
                "ai_consultations": ai_consultations,
                "ts": int(time.time()),
            }, f)
    except Exception:
        pass


def main():
    print("\033[0;36mvirtualOS AI watchdog starting\033[0m")
    print(f"\033[0;90m  check interval: {CHECK_INTERVAL}s (adapts {MIN_INTERVAL}s-{MAX_INTERVAL}s)")
    print(f"  AI calls limit: {MAX_AI_CALLS_PER_HOUR}/hr")
    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    print(f"  AI mode: {'Claude API' if has_api_key else 'rule-based fallback (set ANTHROPIC_API_KEY)'}")
    print(f"  log: {LOG_FILE}\033[0m")
    print()

    try:
        while True:
            run_check()
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print()
        log(f"Stopped. Checks: {total_checks}, Fixes: {total_fixes}, AI calls: {ai_consultations}", "ok")


if __name__ == "__main__":
    main()
