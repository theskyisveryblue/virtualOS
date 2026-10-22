"""Upload Mini-local delta bytes with per-request signatures from the operator.

Only request metadata crosses SSH. R2 secret keys remain on the operator Mac;
the Mini receives signatures restricted to this prefix and payload hash.
"""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import re
import shlex
import sqlite3
import struct
import subprocess
import sys
import threading
from urllib import request

from cloud_disk import BLOCK, digest, validate_config
from cloud_disk_r2_upload import R2UploadStore, upload_delta, pull_delta


def fingerprint(path):
    hasher = hashlib.sha256(b"virtualos-real-delta/v1\0")
    blocks = raw_bytes = 0
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        previous = -1
        for idx, data in db.execute("SELECT idx, data FROM blocks ORDER BY idx"):
            if type(idx) is not int or idx <= previous or not isinstance(data, bytes) or len(data) != BLOCK:
                raise ValueError("invalid source delta block")
            hasher.update(struct.pack(">QQ", idx, len(data)))
            hasher.update(data)
            blocks += 1
            raw_bytes += len(data)
            previous = idx
    return {"source_delta_sha256": hasher.hexdigest(), "source_blocks": blocks,
            "source_raw_bytes": raw_bytes}


def worker(source, config, *, network=None, operation="upload", manifest_sha256=None):
    """Mini side: ask for signatures, then perform HTTPS directly to R2."""
    # These placeholders never authenticate requests; real signatures arrive
    # from the operator. No real credential is copied into this process.
    os.environ.update(CLOUDFLARE_ACCOUNT_ID=config["account_id"],
                      CLOUDFLARE_R2_ENDPOINT=config["endpoint"],
                      AWS_ACCESS_KEY_ID="delegated", AWS_SECRET_ACCESS_KEY="delegated",
                      VOS_R2_DELTA_WORKERS="16", VOS_R2_DELTA_PROGRESS="1")
    os.environ.pop("AWS_SESSION_TOKEN", None)
    pending = {}
    pending_lock = threading.Lock()
    print_lock = threading.Lock()
    counter = 0
    get_signatures = {}
    def emit(row):
        with print_lock:
            print(json.dumps(row, sort_keys=True), flush=True)
    def receive():
        try:
            for line in sys.stdin:
                row = json.loads(line)
                with pending_lock:
                    response = pending.get(row["id"])
                if response is not None:
                    response.put(row)
        finally:
            with pending_lock:
                for response in pending.values():
                    response.put({"error": "operator signer disconnected"})
    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    def transport(unsigned, limit):
        nonlocal counter
        method = unsigned.get_method()
        prefix = config["endpoint"] + "/" + config["bucket"] + "/" + config["object_prefix"] + "/"
        if not unsigned.full_url.startswith(prefix):
            raise ValueError("delegated URL escaped source pin")
        key = unsigned.full_url[len(prefix):]
        with pending_lock:
            cached = get_signatures.pop(key, None) if method == "GET" else None
            counter += 1
            identifier = counter
            response = queue.Queue()
            if cached is None:
                pending[identifier] = response
        if cached is None:
            emit({"delegate_request": {"id": identifier, "method": method,
                  "key": key, "length": len(unsigned.data or b""),
                  "payload_sha256": unsigned.get_header("X-amz-content-sha256")}})
        try:
            signed = response.get(timeout=60) if cached is None else {"headers": cached}
            if "error" in signed:
                raise OSError(signed["error"])
            if "get_headers" in signed:
                with pending_lock:
                    get_signatures[key] = signed["get_headers"]
            actual = request.Request(unsigned.full_url, data=unsigned.data,
                                     headers=signed["headers"], method=method)
            return (network or R2UploadStore._persistent_network)(actual, limit)
        finally:
            with pending_lock:
                pending.pop(identifier, None)
    import cloud_disk_r2_upload as uploader
    def progress(operation, completed, total, **counts):
        if completed % 256 == 0 or completed == total:
            emit({"r2_delta_progress": dict(operation=operation, completed_blocks=completed,
                  total_blocks=total, **counts)})
    uploader._delta_progress = progress
    try:
        store = R2UploadStore(config, transport=transport)
        if operation == "upload":
            before = fingerprint(source)
            emit({"source_delta_fingerprint": before})
            result = upload_delta(source, config, base_label="vm-base.bundle", store=store, workers=16)
            if fingerprint(source) != before:
                raise ValueError("source delta changed during upload")
            emit({"r2_delta_upload": dict(result, **before, signing="operator_only")})
        elif operation == "pull":
            result = pull_delta(config, source, manifest_sha256, store=store, workers=16)
            emit({"r2_delta_pull": dict(result, **fingerprint(source), signing="operator_only")})
        else:
            raise ValueError("invalid delegated operation")
    except Exception as failure:
        emit({"delegate_error": str(failure)})
        receiver.join(timeout=60)
        raise
    receiver.join(timeout=60)


def upload_remote_delta(host, source, config_path, *, remote_tools=None, command=None,
                        operation="upload", manifest_sha256=None):
    """Operator side: validate and sign requests without transmitting secrets."""
    config = config_path if isinstance(config_path, dict) else validate_config(config_path)
    signer = R2UploadStore(config)
    if operation not in ("upload", "pull"):
        raise ValueError("invalid delegated operation")
    if operation == "pull" and (not isinstance(manifest_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256)):
        raise ValueError("pinned delta manifest SHA-256 required")
    if command is None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
            raise ValueError("invalid remote source host")
        tools = remote_tools or "/Users/ai/vos-shared-fleet/tools"
        remote = ["python3", "-u", str(Path(tools) / "cloud_disk_r2_delegate.py"),
                  operation, source, json.dumps(config)]
        if operation == "pull":
            remote.append(manifest_sha256)
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, shlex.join(remote)]
    child_environment = {key: value for key, value in os.environ.items() if key not in
                         ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                          "CLOUDFLARE_API_TOKEN", "CF_API_TOKEN")}
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, bufsize=1, env=child_environment)
    result = None
    remote_error = None
    try:
        for line in process.stdout:
            row = json.loads(line)
            if "delegate_request" in row:
                item = row["delegate_request"]
                key, method, length = item["key"], item["method"], item["length"]
                payload_hash = item["payload_sha256"]
                if (type(item["id"]) is not int or type(length) is not int or
                        method not in ("GET", "PUT") or
                        not re.fullmatch(r"deltas/(?:manifest\.json|objects/[0-9a-f]{64}\.z)", key)):
                    raise ValueError("invalid delegated R2 request")
                if method == "GET":
                    if length != 0 or payload_hash != digest(b""):
                        raise ValueError("invalid delegated GET")
                    extra = None
                else:
                    if operation == "pull":
                        raise ValueError("delegated recovery only permits GET")
                    if not 1 <= length <= (16 * 1024**2 if key.endswith("manifest.json") else BLOCK + 65536):
                        raise ValueError("delegated upload exceeds block bound")
                    if "/objects/" in key and key.rsplit("/", 1)[1][:-2] != payload_hash:
                        raise ValueError("delegated block payload does not match its immutable key")
                    extra = {"if-none-match": "*"}
                signed = signer._request(method, key, extra=extra, payload_sha256=payload_hash)
                reply = {"id": item["id"], "headers": dict(signed.header_items())}
                if method == "PUT":
                    readback = signer._request("GET", key)
                    reply["get_headers"] = dict(readback.header_items())
                process.stdin.write(json.dumps(reply) + "\n")
                process.stdin.flush()
            elif set(row) == {"delegate_error"}:
                remote_error = row["delegate_error"]
                process.stdin.close()
            elif set(row) <= {"source_delta_fingerprint", "r2_delta_progress", "r2_delta_upload", "r2_delta_pull"}:
                print(json.dumps(row, sort_keys=True), flush=True)
                if "r2_delta_" + operation in row:
                    result = row["r2_delta_" + operation]
                    process.stdin.close()
            else:
                raise ValueError("unexpected delegated upload message")
        if process.wait() != 0 or result is None:
            if remote_error:
                raise ValueError("delegated delta failed: " + str(remote_error))
            raise RuntimeError("remote delta upload did not complete")
        return result
    finally:
        process.stdin.close()
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    try:
        if len(sys.argv) not in (4, 5) or sys.argv[1] not in ("upload", "pull"):
            raise ValueError("usage: cloud_disk_r2_delegate.py upload|pull SQLITE CONFIG_JSON [MANIFEST_SHA256]")
        worker(sys.argv[2], json.loads(sys.argv[3]), operation=sys.argv[1],
               manifest_sha256=sys.argv[4] if len(sys.argv) == 5 else None)
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit("delegated_delta: " + str(exc))
