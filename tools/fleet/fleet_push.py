#!/usr/bin/env python3
"""Push the local fleet's status and frames to the Cloudflare relay.

The relay worker (tools/cloudflare/worker) exposes:
    POST /fleet/status     (host auth)  -> stores fleet heartbeat in a DO
    PUT  /fleet/frame/<vm> (host auth)  -> stores a VM frame in R2

This tool reads the local multiplexer/supervisor and republishes:
    tools/fleet/fleet_push.py --relay https://relay.example.com \\
        --supervisor http://127.0.0.1:9200 --token "$VOS_AUTH_TOKEN" --watch

Experimental. Read-only against the local fleet; only writes to the relay.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _fetch(url: str, timeout: float = 10.0) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read() if exc.fp else b""), ""
    except urllib.error.URLError as exc:
        return 0, str(exc.reason).encode(), ""


def _push(method: str, url: str, token: str, body: bytes, ctype: str) -> int:
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ctype,
            # A plain urllib User-Agent is blocked by Cloudflare (error 1010);
            # send a normal one so the relay/WAF accepts the request.
            "User-Agent": "virtualos-fleet-push/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        return 0


def push_once(relay: str, supervisor: str, token: str, verbose: bool) -> int:
    relay = relay.rstrip("/")
    supervisor = supervisor.rstrip("/")

    status_code, status_body, _ = _fetch(f"{supervisor}/fleet/status")
    if status_code != 200:
        print(f"local status unavailable ({status_code})", file=sys.stderr)
        return 1
    posted = _push("POST", f"{relay}/fleet/status", token, status_body, "application/json")
    if verbose:
        print(f"status -> relay {posted}")

    try:
        fleet = json.loads(status_body.decode("utf-8"))
    except ValueError:
        return 1

    pushed_frames = 0
    for inst in fleet.get("instances", []):
        if not inst.get("ready"):
            continue
        name = inst.get("name")
        if not name:
            continue
        code, frame, ctype = _fetch(f"{supervisor}/fleet/frame/{name}")
        if code == 200 and ctype.startswith("image/"):
            fcode = _push("PUT", f"{relay}/fleet/frame/{name}", token, frame, ctype)
            pushed_frames += 1
            if verbose:
                print(f"frame {name} ({len(frame)} B) -> relay {fcode}")

    print(f"pushed status + {pushed_frames} frame(s)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fleet_push")
    parser.add_argument("--relay", required=True, help="relay base URL")
    parser.add_argument("--supervisor", default=os.environ.get("VOS_FLEET_SUPERVISOR", "http://127.0.0.1:9200"))
    parser.add_argument("--token", default=os.environ.get("VOS_AUTH_TOKEN", ""))
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--watch", action="store_true", help="loop forever")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.token:
        print("missing --token or VOS_AUTH_TOKEN", file=sys.stderr)
        return 2

    if not args.watch:
        return push_once(args.relay, args.supervisor, args.token, args.verbose)

    try:
        while True:
            push_once(args.relay, args.supervisor, args.token, args.verbose)
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
