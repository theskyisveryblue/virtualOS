"""Read-only, bounded host-access check; VM relay health is reported separately."""
import datetime
import json
from pathlib import Path
import re
import subprocess
import sys


def run(command, timeout):
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL)
        return {"ok": result.returncode == 0, "exit_code": result.returncode,
                "detail": (result.stdout + result.stderr).strip()[:1800]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "probe timed out"}
    except OSError as error:
        return {"ok": False, "detail": str(error)}


def main():
    root = Path(__file__).resolve().parents[2]
    probes = {}
    emergency = (Path.home() / "Documents/GitHub/realestate-machine/.local"
                 / "mini-remote-20260915/ssh_config")
    routes = []
    if emergency.is_file():
        routes.append(("emergency-cloudflare", "mac-mini-cloudflare", ["-F", str(emergency)]))
    routes.extend((name, name, []) for name in ("mac-mini-ai", "mac-mini-cloudflare"))
    working_route = None
    for label, name, options in routes:
        probes[label] = run([
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=yes", "-o", "ServerAliveInterval=3",
            "-o", "ServerAliveCountMax=1", *options, name,
            "hostname; df -h /System/Volumes/Data"], timeout=12)
        if probes[label]["ok"]:
            working_route = label
            break
    host_access = any(probe["ok"] for probe in probes.values())
    relay = run(["bash", str(root / "tools/fleet/plan-status.sh"), "--quick"], timeout=20)
    # plan-status returns zero even if blocked; inspect the actual health lines.
    health_lines = [line for line in relay.get("detail", "").splitlines()
                    if re.match(r"^(supervisor|relay)\s{2,}", line)]
    relay_live = any(line.startswith("relay ") and "live hb_age=" in line
                     for line in health_lines)
    print(json.dumps({
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host_access_available": host_access,
        "ssh_probes": probes,
        "working_route": working_route,
        "guest_relay_live": relay_live,
        "guest_relay_health": health_lines,
        "relay_probe_completed": relay["ok"],
        "deployment_possible": host_access,
        "note": "A live guest relay does not provide a host shell or prove SSH access."
    }, indent=2))
    return 0 if host_access else 1


if __name__ == "__main__":
    sys.exit(main())
