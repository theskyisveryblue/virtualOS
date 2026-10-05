#!/usr/bin/env python3
"""Check the Mini API without placing its bearer token in process arguments."""

import base64
import binascii
import json
from pathlib import Path
import struct
import sys
import urllib.error
import urllib.request
import zlib


BASE = "http://192.168.64.1:8765"


def png(rgb):
    def chunk(kind, value):
        return (struct.pack(">I", len(value)) + kind + value
                + struct.pack(">I", binascii.crc32(kind + value)))
    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    rows = (b"\x00" + bytes(rgb) * 8) * 8
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def request(path, token, body=None):
    headers = {"Authorization": "Bearer " + token}
    payload = None
    if body is not None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(BASE + path, data=payload, headers=headers)
    with urllib.request.urlopen(call, timeout=5) as response:
        return json.load(response)


def main():
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] != "--full"):
        raise SystemExit("usage: model_api_status.py TOKEN_FILE [--full]")
    token = Path(sys.argv[1]).read_text().strip()
    data = request("/v1/health", token)
    if data.get("status") != "ok":
        raise SystemExit("model_api_check=failed: health")
    if len(sys.argv) == 2:
        print("virtualOS model API healthy on 192.168.64.1:8765")
        return
    routed = request("/v1/route", token, {"task": "Click the open tab"})
    if routed.get("choice") not in {"cli", "structured_api", "dom_click", "form"}:
        raise SystemExit("model_api_check=failed: route")
    critique = request("/v1/critique", token, {
        "text": "The dialog has tiny controls and poor contrast, which makes the action hard to find."})
    if critique.get("critique_model") != "advice_only":
        raise SystemExit("model_api_check=failed: critique")
    left, right = (base64.b64encode(png(rgb)).decode() for rgb in ((255, 0, 0), (0, 0, 255)))
    design = request("/v1/design/compare", token, {
        "left_image_base64": left, "right_image_base64": right})
    if set(design.get("design_principles", {})) != {"alignment", "overlap", "whitespace"}:
        raise SystemExit("model_api_check=failed: design")
    try:
        with urllib.request.urlopen(BASE + "/v1/health", timeout=5):
            pass
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
    else:
        raise SystemExit("model_api_check=failed: unauthorized")
    print("model_api_check=passed route=200 critique=200 design=200 unauthorized=401")


if __name__ == "__main__":
    main()
