#!/usr/bin/env python3
"""Small, reviewable wire format for allowlisted guest AX facts.

VA/v1 carries symbolic IDs and bounded counts only. The transport supplies
nonce, sequence, authentication, and time; this payload is not a trust proof.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys


REGISTRY = {
    "profiles": {"finder_v1": 1, "terminal_v1": 2},
    "states": {"finder_window_present": 1},
    "roles": {"AXMenuBarItem": 1},
    "intents": {"open_finder_window": 1},
    "actions": {"dock_finder_click": 1},
    "verifiers": {"finder_window_present": 1},
}
STATUSES = {"unknown": 0, "false": 1, "true": 2, "error": 3}
KINDS = {"observation": 1, "role_count": 2, "episode": 3}
PROFILE_KINDS = {"finder_v1": {"observation", "episode"},
                 "terminal_v1": {"role_count"}}
TAG = hashlib.sha256(json.dumps({"registry": REGISTRY, "statuses": STATUSES,
    "kinds": KINDS, "profile_kinds": {k: sorted(v) for k, v in PROFILE_KINDS.items()}},
    sort_keys=True, separators=(",", ":")).encode()).digest()[:8]
HEADER = b"VA" + bytes((1,))
BATCH_KIND = 0x80
MAX_BATCH_RECORDS = 64
FRAME_LENGTHS = {1: 20, 2: 16, 3: 21}


def _id(group: str, symbol: str) -> int:
    if type(symbol) is not str:
        raise ValueError(f"unreviewed {group} symbol")
    try:
        return REGISTRY[group][symbol]
    except KeyError as exc:
        raise ValueError(f"unreviewed {group} symbol") from exc


def _symbol(group: str, number: int) -> str:
    matches = [name for name, value in REGISTRY[group].items() if value == number]
    if len(matches) != 1:
        raise ValueError(f"unknown {group} ID")
    return matches[0]


def _byte(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("count or version outside one-byte bound")
    return value


def _status(value: str) -> int:
    if type(value) is not str or value not in STATUSES:
        raise ValueError("unreviewed status")
    return STATUSES[value]


def _error(value: int) -> bytes:
    if type(value) is not int or not -32768 <= value <= 32767:
        raise ValueError("error outside signed 16-bit bound")
    return struct.pack("!h", value)


def encode(record: dict) -> bytes:
    if type(record) is not dict or not isinstance(record.get("kind"), str):
        raise ValueError("record must have a reviewed kind")
    kind = record["kind"]
    if kind not in KINDS:
        raise ValueError("unknown record kind")
    fields = {"observation": {"state", "status", "process_count", "window_count", "error_code"},
              "role_count": {"role", "count"},
              "episode": {"intent", "action", "verifier", "status", "executed", "error_code"}}
    if set(record) != {"kind", "profile", "profile_version"} | fields[kind]:
        raise ValueError("record fields missing or unreviewed")
    profile = _id("profiles", record["profile"])
    version = _byte(record["profile_version"])
    if version != 1 or kind not in PROFILE_KINDS[record["profile"]]:
        raise ValueError("unreviewed profile version or record kind")
    prefix = HEADER + bytes((KINDS[kind],)) + TAG + bytes((profile, version))
    if kind == "observation":
        state = _id("states", record["state"])
        status = _status(record["status"])
        process = _byte(record.get("process_count", 0))
        windows = _byte(record.get("window_count", 0))
        error = record.get("error_code", 0)
        if status in (1, 2):
            if error != 0 or (process == 1 and windows > 0) != (status == 2):
                raise ValueError("observation status contradicts counts")
        elif process or windows or (status == 0 and error) or (status == 3 and not error):
            raise ValueError("unknown/error observation has inconsistent fields")
        return prefix + bytes((state, status, process, windows)) + _error(error)
    if kind == "role_count":
        return prefix + bytes((_id("roles", record["role"]), _byte(record["count"])))
    status = _status(record["status"])
    error = record.get("error_code", 0)
    if (status == 3) != (error != 0):
        raise ValueError("episode error status and code disagree")
    executed = record["executed"]
    if type(executed) is not bool:
        raise ValueError("executed must be boolean")
    return (prefix + bytes((_id("intents", record["intent"]),
                            _id("actions", record["action"]),
                            _id("verifiers", record["verifier"]),
                            status, int(executed))) + _error(error))


def decode(frame: bytes) -> dict:
    if len(frame) < 14 or frame[:3] != HEADER or frame[4:12] != TAG:
        raise ValueError("wire version or registry mismatch")
    kind = frame[3]
    if kind not in FRAME_LENGTHS or len(frame) != FRAME_LENGTHS[kind]:
        raise ValueError("record kind or length invalid")
    profile = _symbol("profiles", frame[12])
    version = frame[13]
    if version != 1:
        raise ValueError("unreviewed profile version")
    base = {"kind": next(name for name, value in KINDS.items() if value == kind),
            "profile": profile, "profile_version": version}
    if kind == 1:
        state, status, process, windows = frame[14:18]
        error = struct.unpack("!h", frame[18:20])[0]
        if status not in STATUSES.values():
            raise ValueError("invalid observation status")
        base.update(state=_symbol("states", state),
                    status=next(name for name, value in STATUSES.items() if value == status),
                    process_count=process, window_count=windows, error_code=error)
    elif kind == 2:
        base.update(role=_symbol("roles", frame[14]), count=frame[15])
    else:
        intent, action, verifier, status, executed = frame[14:19]
        if status not in STATUSES.values() or executed not in (0, 1):
            raise ValueError("invalid episode status")
        base.update(intent=_symbol("intents", intent), action=_symbol("actions", action),
                    verifier=_symbol("verifiers", verifier),
                    status=next(name for name, value in STATUSES.items() if value == status),
                    executed=bool(executed), error_code=struct.unpack("!h", frame[19:21])[0])
    # Canonical re-encoding checks status/count/error invariants as well as IDs.
    if encode(base) != frame:
        raise ValueError("noncanonical wire frame")
    return base


def encode_batch(records: list[dict]) -> bytes:
    """Pack up to 64 canonical records under one version and registry tag.

    This amortizes only the schema tag; the caller still supplies its own
    authenticated transport envelope, sequence, and freshness checks.
    """
    if type(records) is not list or not 1 <= len(records) <= MAX_BATCH_RECORDS:
        raise ValueError("batch must contain 1-64 records")
    payload = bytearray(HEADER + bytes((BATCH_KIND,)) + TAG + bytes((len(records),)))
    for record in records:
        frame = encode(record)
        payload.extend(frame[3:4])   # Reviewed record kind.
        payload.extend(frame[12:])   # Profile, version, and bounded fields.
    return bytes(payload)


def decode_batch(payload: bytes) -> list[dict]:
    if (type(payload) is not bytes or len(payload) < 13 or
            payload[:3] != HEADER or payload[3] != BATCH_KIND or payload[4:12] != TAG):
        raise ValueError("batch version or registry mismatch")
    count = payload[12]
    if not 1 <= count <= MAX_BATCH_RECORDS:
        raise ValueError("batch count outside bound")
    records = []
    offset = 13
    for _ in range(count):
        if offset >= len(payload):
            raise ValueError("truncated batch record")
        kind = payload[offset]
        length = FRAME_LENGTHS.get(kind)
        if length is None or offset + length - 11 > len(payload):
            raise ValueError("unknown or truncated batch record")
        # Individual frame = 12-byte prefix + its profile/fields. The batch
        # stores one kind byte followed by exactly (length - 12) body bytes.
        end = offset + length - 11
        records.append(decode(HEADER + bytes((kind,)) + TAG + payload[offset + 1:end]))
        offset = end
    if offset != len(payload):
        raise ValueError("trailing batch bytes")
    return records


def measure() -> dict:
    fixtures = [
        {"kind": "observation", "profile": "finder_v1", "profile_version": 1,
         "state": "finder_window_present", "status": "false", "process_count": 1,
         "window_count": 0, "error_code": 0},
        {"kind": "observation", "profile": "finder_v1", "profile_version": 1,
         "state": "finder_window_present", "status": "true", "process_count": 1,
         "window_count": 1, "error_code": 0},
        {"kind": "observation", "profile": "finder_v1", "profile_version": 1,
         "state": "finder_window_present", "status": "error", "process_count": 0,
         "window_count": 0, "error_code": -25211},
        {"kind": "role_count", "profile": "terminal_v1", "profile_version": 1,
         "role": "AXMenuBarItem", "count": 7},
        {"kind": "episode", "profile": "finder_v1", "profile_version": 1,
         "intent": "open_finder_window", "action": "dock_finder_click",
         "verifier": "finder_window_present", "status": "true", "executed": True,
         "error_code": 0},
    ]
    rows = []
    for item in fixtures:
        wire = encode(item)
        if decode(wire) != item:
            raise RuntimeError("codec roundtrip failed")
        json_bytes = len(json.dumps(item, sort_keys=True, separators=(",", ":")).encode())
        rows.append({"kind": item["kind"], "status": item.get("status", "count"),
                     "json_bytes": json_bytes, "wire_bytes": len(wire)})
    tampered = bytearray(encode(fixtures[0]))
    tampered[4] ^= 1
    try:
        decode(bytes(tampered))
    except ValueError:
        pass
    else:
        raise RuntimeError("registry mismatch accepted")
    batch = encode_batch(fixtures)
    if decode_batch(batch) != fixtures:
        raise RuntimeError("batch roundtrip failed")
    for bad in (batch[:-1], batch + b"\0", batch[:4] + bytes((batch[4] ^ 1,)) + batch[5:],
                batch[:12] + bytes((0,)) + batch[13:]):
        try:
            decode_batch(bad)
        except ValueError:
            pass
        else:
            raise RuntimeError("invalid batch accepted")
    return {"schema": "VA/1", "registry_sha256_prefix": TAG.hex(),
            "fixtures": rows, "json_total": sum(x["json_bytes"] for x in rows),
            "wire_total": sum(x["wire_bytes"] for x in rows),
            "batch_wire_total": len(batch), "batch_record_count": len(fixtures),
            "payload_only": True, "transport_authentication_included": False}


if __name__ == "__main__":
    if sys.argv[1:] != ["measure"]:
        raise SystemExit("usage: ax_codec.py measure")
    print(json.dumps(measure(), sort_keys=True, separators=(",", ":")))
