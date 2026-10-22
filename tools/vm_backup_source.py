"""Mini-local archive filter: verify uploaded prefix, forward only new bytes."""

import base64
import hashlib
import json
import os
import re
import sys


def read_part(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def forward(source, output, diagnostics, parts):
    checksum = hashlib.sha256()
    tail_checksum = hashlib.sha256()
    size = reused = 0
    for number, part in enumerate(parts, 1):
        if (type(part.get("PartNumber")) is not int or part["PartNumber"] != number or
                type(part.get("Size")) is not int or not 1 <= part["Size"] <= 8 * 1024**2 or
                not isinstance(part.get("ETag"), str) or
                not re.fullmatch(r'"[0-9a-f]{32}"', part["ETag"]) or
                (number < len(parts) and part["Size"] != 8 * 1024**2)):
            raise ValueError("invalid existing part metadata")
        data = read_part(source, part["Size"])
        if (len(data) != part["Size"] or
                '"' + hashlib.md5(data).hexdigest() + '"' != part["ETag"]):
            raise ValueError("archive differs from existing R2 part %s" % number)
        checksum.update(data)
        size += len(data)
    reused = size
    first = read_part(source, 1024 * 1024)
    if parts and parts[-1]["Size"] < 8 * 1024**2 and first:
        raise ValueError("archive continues after a short existing final part")
    # No binary bytes leave the Mini until the entire old prefix matches.
    output.write((json.dumps({"prefix_parts": len(parts), "prefix_bytes": reused}) + "\n").encode())
    output.flush()
    data = first
    while data:
        output.write(data)
        checksum.update(data)
        tail_checksum.update(data)
        size += len(data)
        data = read_part(source, 1024 * 1024)
    output.flush()
    diagnostics.write("vm_resume_source=" + json.dumps({
        "bytes": size, "sha256": checksum.hexdigest(), "reused_bytes": reused,
        "tail_sha256": tail_checksum.hexdigest()}) + "\n")
    diagnostics.flush()


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--parts-fd=3":
            with os.fdopen(3, "rb") as metadata:
                payload = metadata.readline(16 * 1024**2 + 1)
            if len(payload) > 16 * 1024**2 or not payload.endswith(b"\n"):
                raise ValueError("invalid or oversized part-list payload")
            parts = json.loads(payload)
        else:
            parts = json.loads(base64.b64decode(sys.argv[1], validate=True))
        if not isinstance(parts, list) or len(parts) > 10000:
            raise ValueError("invalid part list")
        forward(sys.stdin.buffer, sys.stdout.buffer, sys.stderr, parts)
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise SystemExit("vm_resume_source=failed: %s" % error) from None
