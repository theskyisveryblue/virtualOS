"""Read-only, pinned R2 S3 fetches for immutable cloud-disk objects.

Credentials are environment-only. This module exposes GET operations and never
prints request headers, response bodies, or credential material.
"""

import datetime
import errno
import hashlib
import hmac
import http.client
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, parse, request


CHUNK = 4 * 1024 * 1024
MAX_MANIFEST = 16 * 1024 * 1024
MAX_OBJECT = CHUNK + 65536
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CLOUD_DISK_BUCKET = "virtualos-cloud-disks"


def _hmac(key, value):
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


def _fake_sigv4_valid(method, path, headers, body, access_key, secret_key):
    """Independently check the wire headers received by a disposable fake S3."""
    try:
        authorization = headers.get("Authorization", "")
        credential, signed, signature = authorization.removeprefix("AWS4-HMAC-SHA256 ").split(", ")
        credential = credential.removeprefix("Credential=")
        signed = signed.removeprefix("SignedHeaders=")
        signature = signature.removeprefix("Signature=")
        key, scope = credential.split("/", 1)
        date, region, service, suffix = scope.split("/")
        timestamp = headers.get("x-amz-date", "")
        payload_hash = headers.get("x-amz-content-sha256", "")
        names = signed.split(";")
        if (key != access_key or region != "auto" or service != "s3" or
                suffix != "aws4_request" or date != timestamp[:8] or
                names != sorted(set(names)) or payload_hash != hashlib.sha256(body).hexdigest()):
            return False
        canonical_headers = "".join(f"{name}:{headers.get(name)}\n" for name in names)
        if any(headers.get(name) is None for name in names):
            return False
        canonical = (method + "\n" + path + "\n\n" + canonical_headers +
                     "\n" + signed + "\n" + payload_hash)
        message = ("AWS4-HMAC-SHA256\n" + timestamp + "\n" + scope + "\n" +
                   hashlib.sha256(canonical.encode()).hexdigest())
        signing_key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), date),
                                       region), service), suffix)
        expected = hmac.new(signing_key, message.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (AttributeError, ValueError):
        return False


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = request.build_opener(_NoRedirect)


def _default_transport(signed_request, limit, timeout):
    try:
        with _opener.open(signed_request, timeout=timeout) as response:
            if response.status != 200:
                raise OSError(errno.EIO, "R2 GET returned an unexpected status")
            body = response.read(limit + 1)
    except error.HTTPError as failure:
        try:
            raise OSError(errno.EIO, f"R2 GET failed with HTTP {failure.code}") from None
        finally:
            failure.close()
    except (error.URLError, TimeoutError):
        raise OSError(errno.EIO, "R2 GET transport failed") from None
    if len(body) > limit:
        raise ValueError("R2 object exceeds the bounded read limit")
    return body


class R2ReadOnly:
    """Fetch only manifest.json and SHA-addressed chunks from one pinned prefix."""

    def __init__(self, config, manifest_sha256, *, transport=None):
        account = config["account_id"]
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
        if (not re.fullmatch(r"[0-9a-f]{32}", account) or
                config["endpoint"] != endpoint or
                config["bucket"] != CLOUD_DISK_BUCKET or
                not re.fullmatch(r"[a-z0-9-]+(?:/[a-z0-9-]+)*", config["object_prefix"]) or
                not HEX64.fullmatch(manifest_sha256)):
            raise ValueError("invalid pinned R2 source")
        if (os.environ.get("CLOUDFLARE_ACCOUNT_ID") != account or
                os.environ.get("CLOUDFLARE_R2_ENDPOINT") != endpoint):
            raise ValueError("R2 credentials are not pinned to the configured account")
        self.access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        self.secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        self.session_token = os.environ.get("AWS_SESSION_TOKEN", "")
        if not self.access_key or not self.secret_key:
            raise ValueError("R2 read credentials unavailable")
        self.endpoint = endpoint
        self.host = f"{account}.r2.cloudflarestorage.com"
        self.bucket = config["bucket"]
        self.prefix = config["object_prefix"]
        self.manifest_sha256 = manifest_sha256
        self.transport = transport or _default_transport

    def _signed_request(self, relative_key):
        path = f"/{self.bucket}/{self.prefix}/{relative_key}"
        canonical_uri = parse.quote(path, safe="/-_.~")
        now = datetime.datetime.now(datetime.timezone.utc)
        date = now.strftime("%Y%m%d")
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        headers = {"host": self.host, "x-amz-content-sha256": EMPTY_SHA256,
                   "x-amz-date": timestamp}
        if self.session_token:
            headers["x-amz-security-token"] = self.session_token
        names = sorted(headers)
        signed_headers = ";".join(names)
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in names)
        canonical_request = ("GET\n" + canonical_uri + "\n\n" + canonical_headers +
                             "\n" + signed_headers + "\n" + EMPTY_SHA256)
        scope = f"{date}/auto/s3/aws4_request"
        to_sign = ("AWS4-HMAC-SHA256\n" + timestamp + "\n" + scope + "\n" +
                   hashlib.sha256(canonical_request.encode()).hexdigest())
        signing_key = _hmac(
            _hmac(_hmac(_hmac(("AWS4" + self.secret_key).encode(), date), "auto"), "s3"),
            "aws4_request")
        signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = ("AWS4-HMAC-SHA256 Credential=" + self.access_key + "/" + scope +
                         ", SignedHeaders=" + signed_headers + ", Signature=" + signature)
        headers["authorization"] = authorization
        return request.Request(self.endpoint + canonical_uri, headers=headers, method="GET")

    def _get(self, relative_key, expected_hash, limit):
        body = self.transport(self._signed_request(relative_key), limit, 20)
        if not isinstance(body, bytes) or len(body) > limit or hashlib.sha256(body).hexdigest() != expected_hash:
            raise ValueError("R2 object SHA256 mismatch")
        return body

    def fetch_manifest(self):
        return self._get("manifest.json", self.manifest_sha256, MAX_MANIFEST)

    def fetch_chunk(self, key):
        if not isinstance(key, str) or not HEX64.fullmatch(key):
            raise ValueError("invalid R2 chunk key")
        return self._get(f"objects/{key}.z", key, MAX_OBJECT)


def self_test(directory, config, manifest_hash, open_disk=None):
    """Exercise signed read-only HTTP, pinned paths, bounds, and corruption."""
    manifest = (Path(directory) / "manifest.json").read_bytes()
    import json
    key = next(item["key"] for item in json.loads(manifest)["chunks"] if "key" in item)
    chunk = (Path(directory) / "objects" / f"{key}.z").read_bytes()
    paths = {f"/{config['bucket']}/{config['object_prefix']}/manifest.json": manifest,
             f"/{config['bucket']}/{config['object_prefix']}/objects/{key}.z": chunk}
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not _fake_sigv4_valid("GET", self.path, self.headers, b"",
                                    "fake-access-key", "fake-secret-key"):
                self.send_response(403)
                self.end_headers()
                return
            seen.append((self.path, self.headers.get("Host"), self.headers.get("Authorization", "")))
            body = paths.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            if body is not None:
                self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def fake_transport(signed_request, limit, timeout):
            parsed = parse.urlsplit(signed_request.full_url)
            assert parsed.scheme == "https" and parsed.hostname == f"{config['account_id']}.r2.cloudflarestorage.com"
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
            try:
                client.request("GET", parsed.path, headers=dict(signed_request.header_items()))
                response = client.getresponse()
                if response.status != 200:
                    raise OSError(errno.EIO, "fake R2 GET failed")
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise ValueError("R2 object exceeds the bounded read limit")
                return body
            finally:
                client.close()

        source = R2ReadOnly(config, manifest_hash, transport=fake_transport)
        assert source.fetch_manifest() == manifest
        assert source.fetch_chunk(key) == chunk
        assert len(seen) == 2 and all(row[1] == source.host for row in seen)
        assert all(row[2].startswith("AWS4-HMAC-SHA256 Credential=") for row in seen)
        if open_disk is not None:
            open_disk(fake_transport)
            requests_after_first_open = len(seen)
            open_disk(fake_transport)
            assert len(seen) == requests_after_first_open
        paths[f"/{config['bucket']}/{config['object_prefix']}/objects/{key}.z"] = b"bad"
        try:
            source.fetch_chunk(key)
            raise AssertionError("corrupt R2 chunk accepted")
        except ValueError:
            pass
        try:
            R2ReadOnly(config, "0" * 64, transport=fake_transport).fetch_manifest()
            raise AssertionError("un-pinned R2 manifest accepted")
        except ValueError:
            pass
        server.shutdown()
        worker.join(timeout=5)
    return True
