"""Bounded-memory, write-once upload of a stopped RAW clone to pinned R2.

The manifest is uploaded last. No disk-sized archive or chunk cache is made.
"""

import datetime
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, parse, request
import zlib

from cloud_disk import BLOCK, CHUNK, FORMAT, digest, validate_config
from cloud_disk_r2 import R2ReadOnly, _NoRedirect, _fake_sigv4_valid


EMPTY_SHA256 = digest(b"")
MAX_RAW_BYTES = 64 * 1024**3
MAX_DELTA_BLOCKS = 50000
MAX_DELTA_MANIFEST_BYTES = 16 * 1024**2


class R2RetryableError(OSError):
    """An idempotent object operation may be retried with a fresh signature."""


def _hmac(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


class R2UploadStore:
    """Conditional PUT and bounded GET under one pinned disk bucket and prefix."""

    _connections = threading.local()

    def __init__(self, config, transport=None):
        # Reuse the read path's account/endpoint/credential pin without a GET.
        reader = R2ReadOnly(config, "0" * 64)
        self.endpoint = reader.endpoint
        self.host = reader.host
        self.bucket = reader.bucket
        self.prefix = reader.prefix
        self.access_key = reader.access_key
        self.secret_key = reader.secret_key
        self.session_token = reader.session_token
        self.transport = transport or self._network

    def _request(self, method, key, data=b"", extra=None, *, payload_sha256=None):
        if not re.fullmatch(
                r"(?:manifest\.json|objects/[0-9a-f]{64}\.z"
                r"|deltas/manifest\.json|deltas/objects/[0-9a-f]{64}\.z)", key):
            raise ValueError("invalid R2 upload key")
        path = parse.quote(f"/{self.bucket}/{self.prefix}/{key}", safe="/-_.~")
        now = datetime.datetime.now(datetime.timezone.utc)
        date = now.strftime("%Y%m%d")
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        payload_hash = digest(data) if payload_sha256 is None else payload_sha256
        if not isinstance(payload_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_hash):
            raise ValueError("invalid signed payload hash")
        headers = {"host": self.host, "x-amz-date": timestamp,
                   "x-amz-content-sha256": payload_hash}
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        if extra:
            headers.update(extra)
        names = sorted(headers)
        signed = ";".join(names)
        canonical = (method + "\n" + path + "\n\n" +
                     "".join(f"{name}:{headers[name]}\n" for name in names) +
                     "\n" + signed + "\n" + payload_hash)
        scope = f"{date}/auto/s3/aws4_request"
        message = ("AWS4-HMAC-SHA256\n" + timestamp + "\n" + scope + "\n" +
                   digest(canonical.encode()))
        signing_key = _hmac(
            _hmac(_hmac(_hmac(("AWS4" + self.secret_key).encode(), date), "auto"), "s3"),
            "aws4_request")
        signature = hmac.new(signing_key, message.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = ("AWS4-HMAC-SHA256 Credential=" + self.access_key +
                                    "/" + scope + ", SignedHeaders=" + signed +
                                    ", Signature=" + signature)
        return request.Request(self.endpoint + path, data=data if method == "PUT" else None,
                               headers=headers, method=method)

    @staticmethod
    def _persistent_network(signed_request, limit, *, connection_factory=None):
        """Reuse one verified TLS connection per worker, without redirects."""
        parsed = parse.urlsplit(signed_request.full_url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid persistent R2 endpoint")
        factory = connection_factory or http.client.HTTPSConnection
        connections = getattr(R2UploadStore._connections, "clients", None)
        if connections is None:
            connections = R2UploadStore._connections.clients = {}
        cache_key = (parsed.netloc, factory)
        for attempt in range(2):
            client = connections.get(cache_key)
            if client is None:
                client = connections[cache_key] = factory(parsed.hostname, timeout=30)
            try:
                client.request(signed_request.get_method(), parsed.path,
                               body=signed_request.data, headers=dict(signed_request.header_items()))
                response = client.getresponse()
                status = response.status
                # Error pages need no body. Close rather than interpreting a
                # large 503 page as corrupt object data.
                if status >= 400:
                    client.close()
                    connections.pop(cache_key, None)
                    break
                expected_length = getattr(response, "length", None)
                parts, received = [], 0
                while received <= limit:
                    part = response.read(limit + 1 - received)
                    if not part:
                        break
                    parts.append(part)
                    received += len(part)
                    if getattr(response, "length", None) == 0:
                        break
                body = b"".join(parts)
                # HTTPResponse.read(n) can return a truncated Content-Length
                # body without raising IncompleteRead. Treat framing loss as
                # transport failure before the immutable content hash check.
                if (isinstance(expected_length, int) and expected_length <= limit
                        and received != expected_length):
                    raise http.client.IncompleteRead(b"", expected_length - received)
                if len(body) > limit:
                    client.close()
                    connections.pop(cache_key, None)
                    raise ValueError("R2 response exceeds bounded read limit")
                if response.will_close:
                    client.close()
                    connections.pop(cache_key, None)
                break
            except (OSError, http.client.HTTPException):
                client.close()
                connections.pop(cache_key, None)
                if attempt == 1:
                    raise R2RetryableError(errno.EIO, "R2 request transport failed") from None
        if status == 412 or status == 404 and signed_request.get_method() == "GET":
            return status, b""
        if status >= 400:
            if status in (408, 429, 500, 502, 503, 504):
                raise R2RetryableError(errno.EIO, "R2 service temporarily unavailable")
            raise OSError(errno.EIO, "R2 request failed with HTTP %d" % status)
        return status, body

    @staticmethod
    def _network(signed_request, limit):
        opener = request.build_opener(_NoRedirect)
        try:
            with opener.open(signed_request, timeout=30) as response:
                status = response.status
                body = response.read(limit + 1)
        except error.HTTPError as failure:
            try:
                if failure.code == 412 or failure.code == 404 and signed_request.get_method() == "GET":
                    return failure.code, b""
                raise OSError(errno.EIO, f"R2 request failed with HTTP {failure.code}") from None
            finally:
                failure.close()
        except (error.URLError, TimeoutError, socket.timeout):
            raise OSError(errno.EIO, "R2 request transport failed") from None
        if len(body) > limit:
            raise ValueError("R2 response exceeds bounded read limit")
        return status, body

    def put_if_absent(self, key, data):
        limit = MAX_DELTA_MANIFEST_BYTES if key == "deltas/manifest.json" else CHUNK + 65536
        if len(data) > limit:
            raise ValueError("R2 upload object exceeds its size bound")
        status, _ = self.transport(self._request("PUT", key, data,
                                                {"if-none-match": "*"}), 1024)
        if status == 412:
            return False
        if status not in (200, 201, 204):
            raise OSError(errno.EIO, "R2 conditional PUT failed")
        return True

    def get(self, key, limit):
        body = self.get_if_present(key, limit)
        if body is None:
            raise OSError(errno.ENOENT, "R2 verification object is missing")
        return body

    def get_if_present(self, key, limit):
        status, body = self.transport(self._request("GET", key), limit)
        if status == 404:
            return None
        if status != 200:
            raise OSError(errno.EIO, "R2 verification GET failed")
        return body


def _same_identity(fd, initial):
    current = os.fstat(fd)
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != initial:
        raise OSError(errno.EIO, "RAW clone changed during upload")


def stream_pack_upload(source, store):
    """Upload at most one 4 MiB raw chunk and its compressed form at a time."""
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size < 512 or
                info.st_size > MAX_RAW_BYTES or info.st_size % 512):
            raise ValueError("RAW clone must be a 512-aligned regular file up to 64 GiB")
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        entries = []
        uploaded_bytes = reused_bytes = 0
        for offset in range(0, info.st_size, CHUNK):
            _same_identity(fd, identity)
            expected = min(CHUNK, info.st_size - offset)
            raw = bytearray()
            while len(raw) < expected:
                part = os.pread(fd, expected - len(raw), offset + len(raw))
                if not part:
                    raise OSError(errno.EIO, "short RAW clone read")
                raw.extend(part)
            if raw.count(0) == expected:
                entries.append({"zero": True, "raw_size": expected})
                continue
            encoded = zlib.compress(raw, level=1)
            key = digest(encoded)
            object_key = f"objects/{key}.z"
            was_new = store.put_if_absent(object_key, encoded)
            remote = store.get(object_key, CHUNK + 65536)
            if len(remote) != len(encoded) or digest(remote) != key:
                raise ValueError("uploaded R2 chunk failed SHA256 read-back")
            uploaded_bytes += len(encoded) if was_new else 0
            reused_bytes += 0 if was_new else len(encoded)
            entries.append({"key": key, "sha256": digest(raw),
                            "raw_size": expected, "encoded_size": len(encoded)})
        _same_identity(fd, identity)
        manifest = {"format": FORMAT, "logical_size": info.st_size,
                    "chunk_size": CHUNK, "block_size": BLOCK, "chunks": entries}
        manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        manifest_hash = digest(manifest_bytes)
        if len(manifest_bytes) > 16 * 1024 * 1024:
            raise ValueError("R2 manifest exceeds size bound")
        store.put_if_absent("manifest.json", manifest_bytes)
        if store.get("manifest.json", len(manifest_bytes)) != manifest_bytes:
            raise ValueError("R2 manifest pin conflicts with existing immutable manifest")
        return {"manifest_sha256": manifest_hash, "logical_bytes": info.st_size,
                "chunk_count": len(entries), "object_count": sum("key" in x for x in entries),
                "uploaded_bytes": uploaded_bytes, "reused_bytes": reused_bytes,
                "readback_verified": True}
    finally:
        os.close(fd)


DELTA_FORMAT = "virtualos-shared-delta/v1"


def _delta_workers(workers):
    workers = int(os.environ.get("VOS_R2_DELTA_WORKERS", "1")) if workers is None else workers
    if type(workers) is not int or not 1 <= workers <= 16:
        raise ValueError("delta workers must be between 1 and 16")
    return workers


def _bounded_ordered_map(function, items, workers):
    """Keep at most workers blocks in flight and emit deterministic ordering."""
    if workers == 1:
        for item in items:
            yield function(item)
        return
    items = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        try:
            for _ in range(workers):
                item = next(items, None)
                if item is None:
                    break
                pending.append(pool.submit(function, item))
            while pending:
                yield pending.popleft().result()
                item = next(items, None)
                if item is not None:
                    pending.append(pool.submit(function, item))
        finally:
            for future in pending:
                future.cancel()


def _delta_progress(operation, completed, total, **counts):
    if os.environ.get("VOS_R2_DELTA_PROGRESS") == "1" and (completed % 256 == 0 or completed == total):
        print(json.dumps({"r2_delta_progress": dict(operation=operation,
              completed_blocks=completed, total_blocks=total, **counts)}, sort_keys=True), flush=True)


def upload_delta(delta_sqlite, config_path, *, base_label=None, store=None, workers=None):
    """Upload a shared-base delta to pinned R2 as content-addressed blocks.

    The delta is exactly what `experiments/shared_base.py diff` writes: a SQLite
    `blocks(idx, data)`. Each block is compressed and stored under
    `objects/<sha256-of-encoded>.z` (write-once, so re-uploads reuse objects), and
    a manifest listing them is written last. A fleet therefore stores one base
    (uploaded separately) plus a tiny per-machine delta — never a full disk per
    machine.
    """
    delta_sqlite = Path(delta_sqlite)
    if delta_sqlite.is_symlink() or not delta_sqlite.is_file():
        raise ValueError("delta must be a regular sqlite file")
    config = config_path if isinstance(config_path, dict) else validate_config(config_path)
    if not config["object_prefix"].startswith("fleet/"):
        raise ValueError("delta upload requires a fleet/<machine> object prefix")
    if store is None:
        store = R2UploadStore(config)
    workers = _delta_workers(workers)
    import sqlite3
    db = sqlite3.connect(delta_sqlite.resolve().as_uri() + "?mode=ro", uri=True)
    entries = []
    uploaded_bytes = reused_bytes = 0
    logical = 0
    try:
        db.execute("BEGIN")
        total = db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        if total > MAX_DELTA_BLOCKS:
            raise ValueError("delta has too many changed blocks")
        def checked_rows():
            previous = -1
            for idx, data in db.execute("SELECT idx, data FROM blocks ORDER BY idx"):
                if type(idx) is not int or not previous < idx < MAX_RAW_BYTES // BLOCK:
                    raise ValueError("delta block index out of bounds")
                if not isinstance(data, bytes) or len(data) != BLOCK:
                    raise ValueError("delta block %s must be exactly %s bytes" % (idx, BLOCK))
                previous = idx
                yield idx, data

        def send_block(item):
            idx, data = item
            encoded = zlib.compress(data, level=1)
            key = digest(encoded)
            was_new = False
            for attempt in range(4):
                try:
                    was_new = store.put_if_absent("deltas/objects/%s.z" % key, encoded) or was_new
                    remote = store.get("deltas/objects/%s.z" % key, CHUNK + 65536)
                    break
                except OSError:
                    if attempt == 3:
                        raise
                    time.sleep(0.25 * 2**attempt)
            if len(remote) != len(encoded) or digest(remote) != key:
                raise ValueError("uploaded delta object failed SHA256 read-back")
            return {"idx": idx, "key": key, "sha256": digest(data),
                    "raw_size": len(data), "encoded_size": len(encoded)}, was_new

        for entry, was_new in _bounded_ordered_map(send_block, checked_rows(), workers):
            logical = (entry["idx"] + 1) * BLOCK
            uploaded_bytes += entry["encoded_size"] if was_new else 0
            reused_bytes += 0 if was_new else entry["encoded_size"]
            entries.append(entry)
            _delta_progress("upload", len(entries), total, uploaded_bytes=uploaded_bytes,
                            reused_bytes=reused_bytes)
    finally:
        db.close()
    manifest = {"format": DELTA_FORMAT, "base": base_label, "block_size": BLOCK,
                "highest_changed_byte": logical, "blocks": entries}
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    manifest_hash = digest(manifest_bytes)
    if len(manifest_bytes) > MAX_DELTA_MANIFEST_BYTES:
        raise ValueError("delta manifest exceeds size bound")
    store.put_if_absent("deltas/manifest.json", manifest_bytes)
    if store.get("deltas/manifest.json", len(manifest_bytes)) != manifest_bytes:
        raise ValueError("delta manifest pin conflicts with existing immutable manifest")
    return {"delta_manifest_sha256": manifest_hash, "block_count": len(entries),
            "highest_changed_byte": logical, "uploaded_bytes": uploaded_bytes,
            "reused_bytes": reused_bytes, "readback_verified": True}


def pull_delta(config_path, destination_sqlite, expected_manifest_sha256, *, store=None, workers=None):
    """Download the delta pinned at a config's prefix into a local sqlite.

    Fetches manifest.json, then each block object, verifies every SHA-256, and
    writes a `blocks(idx, data)` sqlite identical in shape to a local diff — so
    the same seed_overlay_from_delta path consumes it unchanged.
    """
    import sqlite3
    config = config_path if isinstance(config_path, dict) else validate_config(config_path)
    if not config["object_prefix"].startswith("fleet/"):
        raise ValueError("delta pull requires a fleet/<machine> object prefix")
    if store is None:
        store = R2UploadStore(config)
    workers = _delta_workers(workers)
    if (not isinstance(expected_manifest_sha256, str) or
            not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256)):
        raise ValueError("pinned delta manifest SHA-256 required")
    manifest_bytes = store.get("deltas/manifest.json", 16 * 1024**2)
    if digest(manifest_bytes) != expected_manifest_sha256:
        raise ValueError("delta manifest does not match pinned SHA-256")
    manifest = json.loads(manifest_bytes)
    if (not isinstance(manifest, dict) or manifest.get("format") != DELTA_FORMAT or
            manifest.get("block_size") != BLOCK or not isinstance(manifest.get("blocks"), list)):
        raise ValueError("invalid delta manifest")
    entries = manifest["blocks"]
    if (type(manifest.get("highest_changed_byte")) is not int or
            not 0 <= manifest["highest_changed_byte"] <= MAX_RAW_BYTES or
            len(entries) > MAX_DELTA_BLOCKS):
        raise ValueError("invalid delta size or block count")
    previous = -1
    for entry in entries:
        if (not isinstance(entry, dict) or
                set(entry) != {"idx", "key", "sha256", "raw_size", "encoded_size"} or
                type(entry["idx"]) is not int or not previous < entry["idx"] < MAX_RAW_BYTES // BLOCK or
                type(entry["raw_size"]) is not int or entry["raw_size"] != BLOCK or
                type(entry["encoded_size"]) is not int or
                not 1 <= entry["encoded_size"] <= CHUNK + 65536 or
                not isinstance(entry["key"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", entry["key"]) or
                not isinstance(entry["sha256"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("invalid delta block entry")
        previous = entry["idx"]
    if manifest["highest_changed_byte"] != (previous + 1) * BLOCK:
        raise ValueError("delta highest changed byte does not match blocks")
    destination = Path(destination_sqlite)
    if not destination.parent.is_dir():
        raise ValueError("delta destination directory does not exist")
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".next.",
                                     suffix=".sqlite", dir=destination.parent)
    os.close(fd)
    db = sqlite3.connect(temporary)
    try:
        db.execute("CREATE TABLE blocks (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        def fetch_block(entry):
            for attempt in range(4):
                try:
                    encoded = store.get("deltas/objects/%s.z" % entry["key"], CHUNK + 65536)
                    break
                except OSError:
                    if attempt == 3:
                        raise
                    time.sleep(0.25 * 2**attempt)
            if len(encoded) != entry["encoded_size"] or digest(encoded) != entry["key"]:
                raise ValueError("delta object hash mismatch")
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(encoded, BLOCK + 1)
            if not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
                raise ValueError("invalid or oversized delta block")
            if len(raw) != entry["raw_size"] or digest(raw) != entry["sha256"]:
                raise ValueError("delta block hash mismatch")
            return entry["idx"], raw

        for number, (idx, raw) in enumerate(_bounded_ordered_map(fetch_block, entries, workers), 1):
            db.execute("INSERT INTO blocks(idx,data) VALUES (?,?)", (idx, raw))
            _delta_progress("pull", number, len(entries))
        db.commit()
        db.close()
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            db.close()
            os.unlink(temporary)
    return {"pulled_blocks": len(manifest["blocks"]), "manifest_sha256": digest(manifest_bytes),
            "destination": str(destination)}


def upload_staging_clone(config_path):
    source = Path.home() / "VirtualOSMachines/.virtualOS-r2-staging.bundle/Disk.img"
    if not (source.parent / ".complete").is_file() or source.is_symlink() or not source.is_file():
        raise ValueError("completed isolated APFS staging clone required")
    production = Path.home() / "VirtualOSMachines/virtualOS.bundle/Disk.img"
    if production.is_file() and os.path.samefile(source, production):
        raise ValueError("refusing to upload the production RAW disk")
    try:
        probe = subprocess.run(["/usr/sbin/lsof", "-F", "p", "--", str(source)],
                               capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("cannot confirm that the RAW clone is stopped") from None
    if probe.returncode != 1 or probe.stdout or probe.stderr:
        raise ValueError("RAW clone must be closed before upload")
    config = validate_config(config_path)
    return stream_pack_upload(source, R2UploadStore(config))


def upload_bundle_clone(store_root, name, config_path):
    """Upload one stopped fleet bundle's RAW to the pinned bucket.

    Accepts any bundle under a lab root, but never the production machine store
    and never a RAW that is currently open. The manifest is written last.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        raise ValueError("invalid bundle name")
    root = Path(store_root).resolve()
    production_root = (Path.home() / "VirtualOSMachines").resolve()
    if root == production_root or str(root).startswith(str(production_root) + os.sep):
        raise ValueError("refusing to upload from the production machine store")
    source = (root / (name + ".bundle")).resolve() / "Disk.img"
    if source.is_symlink() or not source.is_file():
        raise ValueError("bundle disk must be a regular file")
    production = production_root / "virtualOS.bundle/Disk.img"
    if production.is_file() and os.path.samefile(source, production):
        raise ValueError("refusing to upload the production RAW disk")
    try:
        probe = subprocess.run(["/usr/sbin/lsof", "-F", "p", "--", str(source)],
                               capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("cannot confirm that the RAW is stopped") from None
    if probe.returncode != 1 or probe.stdout or probe.stderr:
        raise ValueError("RAW must be closed before upload")
    config = validate_config(config_path)
    return stream_pack_upload(source, R2UploadStore(config))


def self_test(root):
    """Local fake S3 PUT/GET with conditional writes and no credential output."""
    root = Path(root)
    source = root / "stream-source.raw"
    with source.open("wb") as out:
        out.write(bytes(CHUNK))
        out.write(b"end" + bytes(509))
    account = "e" * 32
    config = {"schema": "virtualos-cloud-disk-config/v1", "account_id": account,
              "bucket": "virtualos-cloud-disks", "endpoint": f"https://{account}.r2.cloudflarestorage.com",
              "object_prefix": "fleet/stream-test", "storage_class": "Standard",
              "chunk_size_bytes": CHUNK, "block_size_bytes": BLOCK,
              "local_cache_limit_bytes": CHUNK, "local_overlay_limit_bytes": CHUNK,
              "boot_cache_required": True, "credentials": "environment_only"}
    objects = {}
    events = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_PUT(self):
            events.append(("PUT", self.path))
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if (self.headers.get("If-None-Match") != "*" or
                    not _fake_sigv4_valid("PUT", self.path, self.headers, body,
                                          "fake-access-key", "fake-secret-key")):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path in objects:
                self.send_response(412)
            else:
                objects[self.path] = body
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            events.append(("GET", self.path))
            if not _fake_sigv4_valid("GET", self.path, self.headers, b"",
                                    "fake-access-key", "fake-secret-key"):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = objects.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body) if body is not None else 0))
            self.end_headers()
            if body is not None:
                self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def fake_transport(signed_request, limit):
            parsed = parse.urlsplit(signed_request.full_url)
            assert parsed.scheme == "https" and parsed.hostname == f"{account}.r2.cloudflarestorage.com"
            assert signed_request.get_header("Authorization", "").startswith("AWS4-HMAC-SHA256 ")
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                client.request(signed_request.get_method(), parsed.path,
                               body=signed_request.data,
                               headers=dict(signed_request.header_items()))
                response = client.getresponse()
                status = response.status
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise ValueError("fake response exceeds bound")
                return status, body
            finally:
                client.close()

        from unittest.mock import patch
        with patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": account,
                                     "CLOUDFLARE_R2_ENDPOINT": config["endpoint"],
                                     "AWS_ACCESS_KEY_ID": "fake-access-key",
                                     "AWS_SECRET_ACCESS_KEY": "fake-secret-key"}):
            store = R2UploadStore(config, transport=fake_transport)
            first = stream_pack_upload(source, store)
            assert first["object_count"] == 1 and first["uploaded_bytes"] > 0
            assert len(objects) == 2 and first["readback_verified"]
            manifest_path = f"/{config['bucket']}/{config['object_prefix']}/manifest.json"
            manifest_put = events.index(("PUT", manifest_path))
            assert all("/objects/" in path for method, path in events[:manifest_put]
                       if method == "PUT")
            assert len([1 for method, path in events[:manifest_put]
                        if method == "GET" and "/objects/" in path]) == 1
            second = stream_pack_upload(source, store)
            assert second["manifest_sha256"] == first["manifest_sha256"]
            assert second["uploaded_bytes"] == 0 and second["reused_bytes"] > 0
            object_path = next(key for key in objects if "/objects/" in key)
            original_object = objects[object_path]
            objects[object_path] = b"corrupt"
            try:
                stream_pack_upload(source, store)
                raise AssertionError("corrupted remote object accepted")
            except ValueError:
                pass
            objects[object_path] = original_object
            # A conflicting immutable manifest must fail instead of overwriting.
            original_manifest = objects[manifest_path]
            objects[manifest_path] = b"conflicting-manifest"
            try:
                stream_pack_upload(source, store)
                raise AssertionError("conflicting R2 manifest accepted")
            except ValueError:
                pass
            objects[manifest_path] = original_manifest

            # A real 25,404-block delta has a manifest larger than a RAW chunk.
            # Permit that metadata without weakening the block-object bound.
            manifest_store = R2UploadStore(dict(config, object_prefix="fleet/manifest-bound-test"),
                                           transport=fake_transport)
            large_manifest = json.dumps({"format": DELTA_FORMAT, "base": "vm-base.bundle",
                "block_size": BLOCK, "highest_changed_byte": 25404 * BLOCK,
                "blocks": [{"idx": idx, "key": "a" * 64, "sha256": "b" * 64,
                            "raw_size": BLOCK, "encoded_size": 1024} for idx in range(25404)]},
                separators=(",", ":"), sort_keys=True).encode()
            assert CHUNK + 65536 < len(large_manifest) <= MAX_DELTA_MANIFEST_BYTES
            assert manifest_store.put_if_absent("deltas/manifest.json", large_manifest)
            assert manifest_store.get("deltas/manifest.json", len(large_manifest)) == large_manifest
            for key, length in (("deltas/objects/" + "a" * 64 + ".z", CHUNK + 65537),
                                ("deltas/manifest.json", MAX_DELTA_MANIFEST_BYTES + 1)):
                try:
                    manifest_store.put_if_absent(key, b"x" * length)
                    raise AssertionError("oversized R2 object accepted")
                except ValueError:
                    pass

            # --- shared-base delta: upload -> pull -> identical blocks ---
            import sqlite3
            delta_path = root / "delta.sqlite"
            if delta_path.exists():
                delta_path.unlink()
            delta_db = sqlite3.connect(str(delta_path))
            delta_db.execute("CREATE TABLE blocks (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
            delta_db.execute("INSERT INTO blocks VALUES (?,?)", (3, b"\xab" * BLOCK))
            delta_db.execute("INSERT INTO blocks VALUES (?,?)", (5, b"\xcd" * BLOCK))
            delta_db.commit()
            delta_db.close()
            parallel_counts = {"active": 0, "peak": 0, "retry_injected": False}
            parallel_lock = threading.Lock()
            def measured_transport(signed_request, limit):
                with parallel_lock:
                    parallel_counts["active"] += 1
                    parallel_counts["peak"] = max(parallel_counts["peak"], parallel_counts["active"])
                    fail = (signed_request.get_method() == "GET" and
                            "/deltas/objects/" in signed_request.full_url and
                            not parallel_counts["retry_injected"])
                    if fail:
                        parallel_counts["retry_injected"] = True
                try:
                    time.sleep(0.01)
                    if fail:
                        raise OSError(errno.EIO, "injected transient transport error")
                    return fake_transport(signed_request, limit)
                finally:
                    with parallel_lock:
                        parallel_counts["active"] -= 1
            parallel_store = R2UploadStore(config, transport=measured_transport)
            uploaded = upload_delta(delta_path, config, base_label="fleet-base", store=parallel_store, workers=4)
            assert uploaded["block_count"] == 2 and uploaded["readback_verified"]
            assert parallel_counts["peak"] > 1 and parallel_counts["retry_injected"]
            pulled_path = root / "delta-pulled.sqlite"
            pulled = pull_delta(config, pulled_path, uploaded["delta_manifest_sha256"], store=parallel_store, workers=4)
            assert pulled["pulled_blocks"] == 2
            with sqlite3.connect(str(pulled_path)) as pulled_db:
                got = dict(pulled_db.execute("SELECT idx, data FROM blocks"))
            assert got == {3: b"\xab" * BLOCK, 5: b"\xcd" * BLOCK}, "delta round trip mismatch"
            # Re-upload reuses every immutable object.
            again = upload_delta(delta_path, config, base_label="fleet-base", store=parallel_store, workers=4)
            assert again["uploaded_bytes"] == 0 and again["reused_bytes"] > 0
            assert again["delta_manifest_sha256"] == uploaded["delta_manifest_sha256"]
            # A corrupted remote delta block must be rejected on pull.
            delta_obj = next(key for key in objects if "/deltas/objects/" in key
                             and objects[key] == zlib.compress(b"\xab" * BLOCK, level=1))
            original_delta_obj = objects[delta_obj]
            objects[delta_obj] = b"corrupt"
            try:
                # A failed pull must preserve a previously verified local delta.
                pull_delta(config, pulled_path, uploaded["delta_manifest_sha256"], store=parallel_store, workers=4)
                raise AssertionError("corrupted delta object accepted")
            except ValueError:
                pass
            with sqlite3.connect(str(pulled_path)) as pulled_db:
                assert dict(pulled_db.execute("SELECT idx, data FROM blocks")) == got
            objects[delta_obj] = original_delta_obj
            try:
                pull_delta(config, pulled_path, "0" * 64, store=store)
                raise AssertionError("wrong delta manifest pin accepted")
            except ValueError:
                pass
            # Reject malformed remote metadata before touching the destination.
            delta_manifest_path = f"/{config['bucket']}/{config['object_prefix']}/deltas/manifest.json"
            original_delta_manifest = objects[delta_manifest_path]
            malformed = json.loads(original_delta_manifest)
            malformed["blocks"][0]["idx"] = -1
            objects[delta_manifest_path] = json.dumps(malformed).encode()
            try:
                pull_delta(config, pulled_path, digest(objects[delta_manifest_path]), store=store)
                raise AssertionError("malformed delta manifest accepted")
            except ValueError:
                pass
            objects[delta_manifest_path] = original_delta_manifest
            # Exercise the real stdin/stdout signing protocol in a separate
            # worker process. Its HTTP requests hit this signature-checking
            # server, and no secret is inherited by the worker.
            import sys
            from cloud_disk_r2_delegate import upload_remote_delta, fingerprint
            delegated_config = dict(config, object_prefix="fleet/delegated-test")
            worker_code = '''
import http.client, os, sys
from urllib import parse
sys.path.insert(0, %r)
from cloud_disk_r2_delegate import worker
assert "AWS_SECRET_ACCESS_KEY" not in os.environ
assert "AWS_ACCESS_KEY_ID" not in os.environ
def network(signed, limit):
    client = http.client.HTTPConnection("127.0.0.1", %d, timeout=5)
    try:
        client.request(signed.get_method(), parse.urlsplit(signed.full_url).path,
                       body=signed.data, headers=dict(signed.header_items()))
        response = client.getresponse()
        return response.status, response.read(limit + 1)
    finally:
        client.close()
worker(%r, %r, network=network)
''' % (str(Path(__file__).resolve().parent), server.server_port,
       str(delta_path), delegated_config)
            delegated = upload_remote_delta("unused", str(delta_path), delegated_config,
                command=[sys.executable, "-u", "-c", worker_code])
            assert delegated["readback_verified"] and delegated["signing"] == "operator_only"
            assert delegated["source_delta_sha256"] == fingerprint(delta_path)["source_delta_sha256"]
            delegated_store = R2UploadStore(delegated_config, transport=fake_transport)
            pull_delta(delegated_config, pulled_path, delegated["delta_manifest_sha256"],
                       store=delegated_store, workers=4)
            assert fingerprint(pulled_path) == fingerprint(delta_path)
            pull_code = worker_code.rsplit("worker(", 1)[0] + "worker(%r, %r, network=network, operation='pull', manifest_sha256=%r)\n" % (
                str(pulled_path), delegated_config, delegated["delta_manifest_sha256"])
            downloaded = upload_remote_delta("unused", str(pulled_path), delegated_config,
                operation="pull", manifest_sha256=delegated["delta_manifest_sha256"],
                command=[sys.executable, "-u", "-c", pull_code])
            assert downloaded["source_delta_sha256"] == delegated["source_delta_sha256"]
            before_bad_pin = fingerprint(pulled_path)
            bad_code = worker_code.rsplit("worker(", 1)[0] + "try:\n    worker(%r, %r, network=network, operation='pull', manifest_sha256=%r)\nexcept ValueError:\n    sys.exit(1)\n" % (
                str(pulled_path), delegated_config, "0" * 64)
            try:
                upload_remote_delta("unused", str(pulled_path), delegated_config,
                    operation="pull", manifest_sha256="0" * 64,
                    command=[sys.executable, "-u", "-c", bad_code])
                raise AssertionError("delegated recovery accepted a wrong manifest pin")
            except ValueError:
                pass
            assert fingerprint(pulled_path) == before_bad_pin
            connections_created = []
            def local_connection(host, timeout):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
                connections_created.append(client)
                return client
            def persistent_transport(signed, limit):
                return R2UploadStore._persistent_network(signed, limit, connection_factory=local_connection)
            persistent = R2UploadStore(config, transport=persistent_transport)
            assert persistent.get("manifest.json", 4096) == original_manifest
            assert persistent.get("manifest.json", 4096) == original_manifest
            assert len(connections_created) == 1, "persistent R2 transport did not reuse its connection"
            try:
                persistent.get("manifest.json", 1)
                raise AssertionError("persistent transport exceeded its read bound")
            except ValueError:
                pass
            assert persistent.get("manifest.json", 4096) == original_manifest
            assert len(connections_created) == 2, "oversized response poisoned reused connection"
            for client in connections_created:
                client.close()
            R2UploadStore._connections.clients.pop((persistent.host, local_connection), None)
        server.shutdown()
        worker.join(timeout=5)
    assert any(method == "PUT" for method, _ in events)
    assert any(method == "GET" for method, _ in events)
    return True
