"""Bounded-memory, write-once upload of a stopped RAW clone to pinned R2.

The manifest is uploaded last. No disk-sized archive or chunk cache is made.
"""

import datetime
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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, parse, request
import zlib

from cloud_disk import BLOCK, CHUNK, FORMAT, digest, validate_config
from cloud_disk_r2 import R2ReadOnly, _NoRedirect, _fake_sigv4_valid


EMPTY_SHA256 = digest(b"")
MAX_RAW_BYTES = 64 * 1024**3


def _hmac(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


class R2UploadStore:
    """Conditional PUT and bounded GET under one pinned disk bucket and prefix."""

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

    def _request(self, method, key, data=b"", extra=None):
        if not re.fullmatch(r"(?:manifest\.json|objects/[0-9a-f]{64}\.z)", key):
            raise ValueError("invalid R2 upload key")
        path = parse.quote(f"/{self.bucket}/{self.prefix}/{key}", safe="/-_.~")
        now = datetime.datetime.now(datetime.timezone.utc)
        date = now.strftime("%Y%m%d")
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        payload_hash = digest(data)
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
    def _network(signed_request, limit):
        opener = request.build_opener(_NoRedirect)
        try:
            with opener.open(signed_request, timeout=30) as response:
                status = response.status
                body = response.read(limit + 1)
        except error.HTTPError as failure:
            try:
                if failure.code == 412:
                    return 412, b""
                raise OSError(errno.EIO, f"R2 request failed with HTTP {failure.code}") from None
            finally:
                failure.close()
        except (error.URLError, TimeoutError, socket.timeout):
            raise OSError(errno.EIO, "R2 request transport failed") from None
        if len(body) > limit:
            raise ValueError("R2 response exceeds bounded read limit")
        return status, body

    def put_if_absent(self, key, data):
        if len(data) > CHUNK + 65536:
            raise ValueError("R2 upload object exceeds chunk bound")
        status, _ = self.transport(self._request("PUT", key, data,
                                                {"if-none-match": "*"}), 1024)
        if status == 412:
            return False
        if status not in (200, 201, 204):
            raise OSError(errno.EIO, "R2 conditional PUT failed")
        return True

    def get(self, key, limit):
        status, body = self.transport(self._request("GET", key), limit)
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
              "object_prefix": "disks/stream-test", "storage_class": "Standard",
              "chunk_size_bytes": CHUNK, "block_size_bytes": BLOCK,
              "local_cache_limit_bytes": CHUNK, "local_overlay_limit_bytes": CHUNK,
              "boot_cache_required": True, "credentials": "environment_only"}
    objects = {}
    events = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            events.append(("PUT", self.path))
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if (self.headers.get("If-None-Match") != "*" or
                    not _fake_sigv4_valid("PUT", self.path, self.headers, body,
                                          "fake-access-key", "fake-secret-key")):
                self.send_response(403)
                self.end_headers()
                return
            if self.path in objects:
                self.send_response(412)
            else:
                objects[self.path] = body
                self.send_response(200)
            self.end_headers()

        def do_GET(self):
            events.append(("GET", self.path))
            if not _fake_sigv4_valid("GET", self.path, self.headers, b"",
                                    "fake-access-key", "fake-secret-key"):
                self.send_response(403)
                self.end_headers()
                return
            body = objects.get(self.path)
            self.send_response(200 if body is not None else 404)
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
        server.shutdown()
        worker.join(timeout=5)
    assert any(method == "PUT" for method, _ in events)
    assert any(method == "GET" for method, _ in events)
    return True
