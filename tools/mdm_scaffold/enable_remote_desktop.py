#!/usr/bin/env python3
"""
Scaffold for Apple's MDM EnableRemoteDesktop command.

This script can:
- Generate the XML plist payload for EnableRemoteDesktop
- Optionally send the command to an MDM endpoint (PUT)
"""

from __future__ import annotations

import argparse
import plistlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, Tuple


DEFAULT_COMMAND_UUID = "0001_EnableRemoteDesktop"
MDM_CONTENT_TYPE = "application/x-apple-aspen-mdm"


def build_enable_remote_desktop_command(command_uuid: str = DEFAULT_COMMAND_UUID) -> dict:
    if not command_uuid.strip():
        raise ValueError("command_uuid must not be empty")
    return {
        "Command": {
            "RequestType": "EnableRemoteDesktop",
        },
        "CommandUUID": command_uuid.strip(),
    }


def command_to_plist_bytes(command: dict) -> bytes:
    return plistlib.dumps(command, fmt=plistlib.FMT_XML, sort_keys=False)


def parse_headers(raw_headers: Iterable[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for raw in raw_headers:
        if ":" not in raw:
            raise ValueError(f"invalid header format: {raw!r}, expected 'Key: Value'")
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"invalid header key in {raw!r}")
        headers[key] = value
    return headers


def validate_mdm_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("mdm URL must start with http:// or https://")
    if not parsed.netloc:
        raise ValueError("mdm URL must include host")
    return url


def send_enable_remote_desktop(
    mdm_url: str,
    command_uuid: str = DEFAULT_COMMAND_UUID,
    headers: Dict[str, str] | None = None,
    timeout: float = 15.0,
) -> Tuple[int, str]:
    validated_url = validate_mdm_url(mdm_url)
    command = build_enable_remote_desktop_command(command_uuid=command_uuid)
    payload = command_to_plist_bytes(command)

    request_headers = {"Content-Type": MDM_CONTENT_TYPE}
    if headers:
        request_headers.update(headers)

    req = urllib.request.Request(
        validated_url,
        data=payload,
        headers=request_headers,
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        return int(response.status), body


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scaffold Apple MDM EnableRemoteDesktop command")
    sub = parser.add_subparsers(dest="mode", required=True)

    payload = sub.add_parser("payload", help="generate EnableRemoteDesktop plist payload")
    payload.add_argument("--uuid", default=DEFAULT_COMMAND_UUID, help="command UUID")
    payload.add_argument("--output", default="-", help="output file path or '-' for stdout")

    send = sub.add_parser("send", help="send EnableRemoteDesktop to an MDM endpoint")
    send.add_argument("--url", required=True, help="MDM endpoint URL (PUT)")
    send.add_argument("--uuid", default=DEFAULT_COMMAND_UUID, help="command UUID")
    send.add_argument(
        "--header",
        action="append",
        default=[],
        help="extra request header, e.g. 'Authorization: Bearer ...'",
    )
    send.add_argument("--dry-run", action="store_true", help="print payload and request metadata only")
    return parser


def cmd_payload(args: argparse.Namespace) -> int:
    payload = command_to_plist_bytes(build_enable_remote_desktop_command(command_uuid=args.uuid))
    if args.output == "-":
        sys.stdout.buffer.write(payload)
        if not payload.endswith(b"\n"):
            sys.stdout.write("\n")
        return 0

    with open(args.output, "wb") as f:
        f.write(payload)
    print(f"wrote payload to {args.output}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    extra_headers = parse_headers(args.header)
    payload = command_to_plist_bytes(build_enable_remote_desktop_command(command_uuid=args.uuid))
    validate_mdm_url(args.url)

    if args.dry_run:
        print("dry-run: would send EnableRemoteDesktop command")
        print(f"url: {args.url}")
        print(f"headers: {{'Content-Type': '{MDM_CONTENT_TYPE}', **{extra_headers}}}")
        print(payload.decode("utf-8", errors="replace"))
        return 0

    try:
        status, body = send_enable_remote_desktop(
            mdm_url=args.url,
            command_uuid=args.uuid,
            headers=extra_headers,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"http error: {exc.code}\n{body}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return 2

    print(f"status: {status}")
    print(body)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == "payload":
        return cmd_payload(args)
    if args.mode == "send":
        return cmd_send(args)
    parser.error("invalid mode")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

