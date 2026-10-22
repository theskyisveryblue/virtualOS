"""Bounded overlay checkpoint prototype with optional immutable R2 transfer.

All file paths stay under /private/tmp. Remote transfer accepts a caller's
pinned R2 store/reader, allowing fake S3 tests without production credentials.
"""

import errno
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import zlib

from cloud_disk import BLOCK, MAX_MANIFEST_BYTES, CloudDisk, digest, lab_path


FORMAT = "virtualos-overlay-checkpoint/v1"
MAX_BYTES = 16 * 1024**2
MAX_BLOCKS = MAX_BYTES // BLOCK
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _finish_staging_journal(path):
    # SQLite versions differ in when they unlink clean WAL sidecars. The
    # staging database has no other readers, so seal it as one SQLite file.
    with sqlite3.connect(str(path), timeout=30) as db:
        mode = db.execute("PRAGMA journal_mode=DELETE").fetchone()
        if mode != ("delete",):
            raise OSError(errno.EIO, "checkpoint restore journal did not close")
    if Path(str(path) + "-wal").exists():
        raise OSError(errno.EIO, "checkpoint restore WAL did not close")
    # macOS SQLite 3.9 can leave an unused shared-memory sidecar after the
    # final connection closes. No connection can use this private staging DB.
    Path(str(path) + "-shm").unlink(missing_ok=True)


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def _lab_path(path):
    original = Path(path)
    if original.is_symlink():
        raise ValueError("checkpoint paths must not be symlinks")
    return lab_path(path)


def _private_parent(path):
    parent = path.parent
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o077):
        raise ValueError("checkpoint output parent must be a private directory")


def _read_regular(path, limit):
    # Check the opened descriptor: a path check followed by ordinary open can
    # race a symlink replacement, and opening a FIFO can block indefinitely.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or info.st_mode & 0o022):
            raise ValueError("checkpoint input must be an owned regular file")
        return stream.read(limit + 1)


def _base_identity(base_dir, scratch_parent):
    fd, probe_path = tempfile.mkstemp(prefix="vos-checkpoint-base-", dir=scratch_parent)
    os.close(fd)
    probe = None
    try:
        probe = CloudDisk(base_dir, probe_path, min_host_free_bytes=0)
        pinned = probe.db.execute(
            "SELECT value FROM metadata WHERE name='manifest_sha256'").fetchone()[0]
        with (base_dir / "manifest.json").open("rb") as stream:
            manifest_bytes = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES or digest(manifest_bytes) != pinned:
            raise ValueError("immutable base manifest changed during checkpoint")
        return pinned, probe.size
    finally:
        if probe is not None:
            probe.close()
        Path(probe_path).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(probe_path + suffix).unlink(missing_ok=True)


def snapshot_overlay(base_dir, overlay, destination, max_bytes=MAX_BYTES):
    """Publish a consistent SQLite overlay snapshot after all objects fsync."""
    base_dir, overlay, destination = map(_lab_path, (base_dir, overlay, destination))
    if not 1 <= max_bytes <= MAX_BYTES or destination.exists() or not overlay.is_file():
        raise ValueError("checkpoint destination or byte limit is invalid")
    _private_parent(destination)
    base_hash, size = _base_identity(base_dir, destination.parent)
    reader = sqlite3.connect(overlay.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    staging = None
    try:
        reader.execute("PRAGMA query_only=ON")
        reader.execute("BEGIN")
        identity = reader.execute(
            "SELECT value FROM metadata WHERE name='manifest_sha256'").fetchone()
        count, raw_bytes = reader.execute(
            "SELECT COUNT(*), COALESCE(SUM(length(data)),0) FROM blocks").fetchone()
        if (identity != (base_hash,) or count > MAX_BLOCKS or raw_bytes > max_bytes or
                count * BLOCK > max_bytes + BLOCK):
            raise ValueError("overlay identity or checkpoint size bound failed")
        staging = Path(tempfile.mkdtemp(prefix=destination.name + ".next-",
                                        dir=destination.parent))
        os.chmod(staging, 0o700)
        objects = staging / "objects"
        objects.mkdir(mode=0o700)
        entries = []
        total_encoded = 0
        for index, raw in reader.execute("SELECT idx,data FROM blocks ORDER BY idx"):
            if type(index) is not int or not 0 <= index < (size + BLOCK - 1) // BLOCK:
                raise ValueError("overlay block index invalid")
            expected = min(BLOCK, size - index * BLOCK)
            if len(raw) != expected:
                raise ValueError("overlay block length invalid")
            encoded = zlib.compress(raw, level=1)
            key = digest(encoded)
            total_encoded += len(encoded)
            if total_encoded > max_bytes:
                raise ValueError("checkpoint encoded byte limit exceeded")
            object_path = objects / (key + ".z")
            if not object_path.exists():
                _write_new(object_path, encoded)
            entries.append({"idx": index, "raw_size": expected,
                            "raw_sha256": digest(raw), "key": key,
                            "encoded_size": len(encoded)})
        _fsync_dir(objects)
        manifest = {"format": FORMAT, "base_manifest_sha256": base_hash,
                    "logical_size": size, "block_size": BLOCK, "blocks": entries}
        manifest_bytes = (json.dumps(manifest, sort_keys=True,
                                     separators=(",", ":")) + "\n").encode()
        if total_encoded + len(manifest_bytes) > max_bytes:
            raise ValueError("checkpoint total byte limit exceeded")
        _write_new(staging / "manifest.json", manifest_bytes)
        _fsync_dir(staging)
        if destination.exists():
            raise FileExistsError(destination)
        os.rename(staging, destination)
        staging = None
        _fsync_dir(destination.parent)
        return {"checkpoint_sha256": digest(manifest_bytes),
                "base_manifest_sha256": base_hash, "blocks": count,
                "encoded_bytes": total_encoded}
    finally:
        try:
            reader.close()
        finally:
            if staging is not None:
                shutil.rmtree(staging)


def restore_overlay(snapshot, base_dir, destination, expected_sha256,
                    max_bytes=MAX_BYTES):
    """Verify a pinned snapshot and publish a new SQLite overlay atomically."""
    snapshot, base_dir, destination = map(_lab_path, (snapshot, base_dir, destination))
    if (not HEX64.fullmatch(expected_sha256) or not 1 <= max_bytes <= MAX_BYTES or
            destination.exists() or not snapshot.is_dir() or
            (snapshot / "manifest.json").is_symlink() or
            (snapshot / "objects").is_symlink()):
        raise ValueError("invalid checkpoint restore source or destination")
    _private_parent(destination)
    manifest_bytes = _read_regular(snapshot / "manifest.json", max_bytes)
    if len(manifest_bytes) > max_bytes or digest(manifest_bytes) != expected_sha256:
        raise ValueError("checkpoint manifest hash mismatch")
    manifest = json.loads(manifest_bytes)
    base_hash, size = _base_identity(base_dir, destination.parent)
    if (not isinstance(manifest, dict) or set(manifest) !=
            {"format", "base_manifest_sha256", "logical_size", "block_size", "blocks"} or
            manifest["format"] != FORMAT or manifest["base_manifest_sha256"] != base_hash or
            manifest["logical_size"] != size or manifest["block_size"] != BLOCK or
            not isinstance(manifest["blocks"], list) or
            len(manifest["blocks"]) > MAX_BLOCKS):
        raise ValueError("checkpoint base identity or schema mismatch")
    staging = destination.with_name(destination.name + ".next." + str(os.getpid()))
    if staging.exists() or staging.is_symlink():
        raise ValueError("checkpoint restore staging path already exists")
    written = 0
    disk = None
    try:
        disk = CloudDisk(base_dir, staging, min_host_free_bytes=0)
        last_index = -1
        with disk.db:
            for entry in manifest["blocks"]:
                if (not isinstance(entry, dict) or set(entry) !=
                        {"idx", "raw_size", "raw_sha256", "key", "encoded_size"} or
                        type(entry["idx"]) is not int or entry["idx"] <= last_index or
                        entry["idx"] >= (size + BLOCK - 1) // BLOCK or
                        type(entry["raw_size"]) is not int or
                        entry["raw_size"] != min(BLOCK, size - entry["idx"] * BLOCK) or
                        type(entry["encoded_size"]) is not int or
                        not 1 <= entry["encoded_size"] <= BLOCK + 64 or
                        not isinstance(entry["key"], str) or not HEX64.fullmatch(entry["key"]) or
                        not isinstance(entry["raw_sha256"], str) or
                        not HEX64.fullmatch(entry["raw_sha256"])):
                    raise ValueError("checkpoint block schema invalid")
                key = entry["key"]
                object_path = snapshot / "objects" / (key + ".z")
                if object_path.is_symlink():
                    raise ValueError("checkpoint object symlink refused")
                encoded = _read_regular(object_path, BLOCK + 64)
                if (len(encoded) != entry["encoded_size"] or digest(encoded) != key):
                    raise ValueError("checkpoint object hash mismatch")
                decoder = zlib.decompressobj()
                raw = decoder.decompress(encoded, entry["raw_size"] + 1)
                if (len(raw) != entry["raw_size"] or not decoder.eof or
                        decoder.unconsumed_tail or decoder.unused_data or
                        digest(raw) != entry["raw_sha256"]):
                    raise ValueError("checkpoint block hash mismatch")
                written += len(encoded)
                if written + len(manifest_bytes) > max_bytes:
                    raise ValueError("checkpoint restore byte limit exceeded")
                disk.db.execute("INSERT INTO blocks(idx,data) VALUES (?,?)",
                                (entry["idx"], raw))
                last_index = entry["idx"]
        disk.close()
        disk = None
        _finish_staging_journal(staging)
        if any(Path(str(staging) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise OSError(errno.EIO, "checkpoint restore WAL did not close cleanly")
        verified = CloudDisk(base_dir, staging, min_host_free_bytes=0)
        try:
            if verified.db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise OSError(errno.EIO, "restored SQLite overlay failed quick_check")
            for entry in manifest["blocks"]:
                row = verified.db.execute("SELECT data FROM blocks WHERE idx=?",
                                          (entry["idx"],)).fetchone()
                if row is None or digest(row[0]) != entry["raw_sha256"]:
                    raise OSError(errno.EIO, "restored SQLite block failed verification")
        finally:
            verified.close()
        _finish_staging_journal(staging)
        if any(Path(str(staging) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise OSError(errno.EIO, "verified SQLite overlay left a WAL sidecar")
        os.chmod(staging, 0o600)
        os.link(staging, destination)  # Atomic create; never overwrite an overlay.
        _fsync_dir(destination.parent)
        return {"restored_blocks": len(manifest["blocks"]),
                "base_manifest_sha256": base_hash,
                "checkpoint_sha256": expected_sha256}
    finally:
        try:
            if disk is not None:
                disk.close()
        finally:
            staging.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(staging) + suffix).unlink(missing_ok=True)


def _checkpoint_prefix(endpoint, manifest_sha256):
    # One manifest per prefix makes conditional PUT a write-once checkpoint.
    if (not HEX64.fullmatch(manifest_sha256) or
            not endpoint.prefix.endswith("/checkpoints/" + manifest_sha256)):
        raise ValueError("R2 checkpoint requires its own content-pinned prefix")


def upload_checkpoint(snapshot, base_dir, store, expected_sha256,
                      max_bytes=MAX_BYTES):
    """Verify and publish local checkpoint objects, then its manifest last."""
    snapshot, base_dir = map(_lab_path, (snapshot, base_dir))
    _checkpoint_prefix(store, expected_sha256)
    if not 1 <= max_bytes <= MAX_BYTES or not snapshot.is_dir():
        raise ValueError("invalid checkpoint upload source or byte bound")
    if (snapshot / "manifest.json").is_symlink() or (snapshot / "objects").is_symlink():
        raise ValueError("checkpoint upload symlink refused")
    manifest_bytes = _read_regular(snapshot / "manifest.json", max_bytes)
    if len(manifest_bytes) > max_bytes or digest(manifest_bytes) != expected_sha256:
        raise ValueError("checkpoint upload manifest pin mismatch")
    manifest = json.loads(manifest_bytes)
    base_hash, size = _base_identity(base_dir, snapshot.parent)
    if (not isinstance(manifest, dict) or set(manifest) !=
            {"format", "base_manifest_sha256", "logical_size", "block_size", "blocks"} or
            manifest["format"] != FORMAT or manifest["base_manifest_sha256"] != base_hash or
            manifest["logical_size"] != size or manifest["block_size"] != BLOCK or
            not isinstance(manifest["blocks"], list) or len(manifest["blocks"]) > MAX_BLOCKS):
        raise ValueError("checkpoint upload base identity or schema mismatch")
    previous = -1
    total = len(manifest_bytes)
    uploaded = reused = 0
    for entry in manifest["blocks"]:
        if (not isinstance(entry, dict) or set(entry) !=
                {"idx", "raw_size", "raw_sha256", "key", "encoded_size"} or
                type(entry["idx"]) is not int or entry["idx"] <= previous or
                entry["idx"] >= (size + BLOCK - 1) // BLOCK or
                type(entry["raw_size"]) is not int or
                entry["raw_size"] != min(BLOCK, size - entry["idx"] * BLOCK) or
                type(entry["encoded_size"]) is not int or
                not 1 <= entry["encoded_size"] <= BLOCK + 64 or
                not isinstance(entry["key"], str) or not HEX64.fullmatch(entry["key"]) or
                not isinstance(entry["raw_sha256"], str) or
                not HEX64.fullmatch(entry["raw_sha256"])):
            raise ValueError("checkpoint upload block schema invalid")
        previous = entry["idx"]
        total += entry["encoded_size"]
        if total > max_bytes:
            raise ValueError("checkpoint upload byte limit exceeded")
        path = snapshot / "objects" / (entry["key"] + ".z")
        if path.is_symlink():
            raise ValueError("checkpoint upload object symlink refused")
        encoded = _read_regular(path, BLOCK + 64)
        if len(encoded) != entry["encoded_size"] or digest(encoded) != entry["key"]:
            raise ValueError("checkpoint upload object hash mismatch")
        decoder = zlib.decompressobj()
        raw = decoder.decompress(encoded, entry["raw_size"] + 1)
        if (len(raw) != entry["raw_size"] or not decoder.eof or
                decoder.unconsumed_tail or decoder.unused_data or
                digest(raw) != entry["raw_sha256"]):
            raise ValueError("checkpoint upload raw block hash mismatch")
        key = "objects/" + entry["key"] + ".z"
        was_new = store.put_if_absent(key, encoded)
        if store.get(key, BLOCK + 64) != encoded:
            raise ValueError("checkpoint upload object readback mismatch")
        uploaded += len(encoded) if was_new else 0
        reused += 0 if was_new else len(encoded)
    store.put_if_absent("manifest.json", manifest_bytes)
    if store.get("manifest.json", len(manifest_bytes)) != manifest_bytes:
        raise ValueError("checkpoint upload immutable manifest conflict")
    return {"checkpoint_sha256": expected_sha256, "blocks": len(manifest["blocks"]),
            "uploaded_bytes": uploaded, "reused_bytes": reused,
            "manifest_last": True, "readback_verified": True}


def restore_remote_checkpoint(base_dir, destination, reader, expected_sha256,
                              max_bytes=MAX_BYTES):
    """Fetch pinned R2 checkpoint into a bounded private staging dir and restore."""
    base_dir, destination = map(_lab_path, (base_dir, destination))
    _checkpoint_prefix(reader, expected_sha256)
    if reader.manifest_sha256 != expected_sha256 or not 1 <= max_bytes <= MAX_BYTES:
        raise ValueError("checkpoint restore pin or byte bound invalid")
    if destination.exists() or destination.is_symlink():
        raise ValueError("checkpoint restore destination already exists")
    _private_parent(destination)
    staging = Path(tempfile.mkdtemp(prefix="vos-r2-checkpoint-", dir=destination.parent))
    os.chmod(staging, 0o700)
    try:
        manifest_bytes = reader.fetch_manifest()
        if len(manifest_bytes) > max_bytes or digest(manifest_bytes) != expected_sha256:
            raise ValueError("remote checkpoint manifest size or hash mismatch")
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("blocks"), list):
            raise ValueError("remote checkpoint manifest schema invalid")
        if len(manifest["blocks"]) > MAX_BLOCKS:
            raise ValueError("remote checkpoint block count exceeds bound")
        objects = staging / "objects"
        objects.mkdir(mode=0o700)
        total = len(manifest_bytes)
        for entry in manifest["blocks"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("key"), str) or
                    not HEX64.fullmatch(entry["key"]) or
                    type(entry.get("encoded_size")) is not int or
                    not 1 <= entry["encoded_size"] <= BLOCK + 64):
                raise ValueError("remote checkpoint object schema invalid")
            total += entry["encoded_size"]
            if total > max_bytes:
                raise ValueError("remote checkpoint byte limit exceeded")
            path = objects / (entry["key"] + ".z")
            if path.exists():
                continue
            encoded = reader.fetch_chunk(entry["key"])
            if len(encoded) != entry["encoded_size"]:
                raise ValueError("remote checkpoint object length mismatch")
            _write_new(path, encoded)
        _write_new(staging / "manifest.json", manifest_bytes)
        return restore_overlay(staging, base_dir, destination, expected_sha256,
                               max_bytes=max_bytes)
    finally:
        shutil.rmtree(staging)


def self_test_remote(root, base_dir, source_overlay, snapshot, checkpoint_hash,
                     expected_bytes):
    """Use signed fake R2 calls only; no network or account mutations."""
    from urllib import parse
    from unittest.mock import patch
    from cloud_disk_r2 import R2ReadOnly, _fake_sigv4_valid
    from cloud_disk_r2_upload import R2UploadStore

    account = "d" * 32
    config = {"account_id": account, "bucket": "virtualos-cloud-disks",
              "endpoint": f"https://{account}.r2.cloudflarestorage.com",
              "object_prefix": "disks/overlay-test/checkpoints/" + checkpoint_hash}
    objects, events = {}, []

    def transport(signed_request, limit):
        parsed = parse.urlsplit(signed_request.full_url)
        assert parsed.scheme == "https" and parsed.hostname == f"{account}.r2.cloudflarestorage.com"
        method = signed_request.get_method()
        body = signed_request.data if method == "PUT" else b""
        headers = {key.lower(): value for key, value in signed_request.header_items()}
        headers["Authorization"] = headers.get("authorization", "")
        assert _fake_sigv4_valid(method, parsed.path, headers, body,
                                 "fake-access-key", "fake-secret-key")
        events.append((method, parsed.path))
        if method == "PUT":
            assert headers.get("if-none-match") == "*"
            if parsed.path in objects:
                return 412, b""
            objects[parsed.path] = body
            return 200, b""
        if parsed.path not in objects:
            return 404, b""
        result = objects[parsed.path]
        if len(result) > limit:
            raise ValueError("fake checkpoint response exceeds bound")
        return 200, result

    def read_transport(signed_request, limit, _timeout):
        status, body = transport(signed_request, limit)
        if status != 200:
            raise OSError(errno.EIO, "fake checkpoint object missing")
        return body

    with patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": account,
                              "CLOUDFLARE_R2_ENDPOINT": config["endpoint"],
                              "AWS_ACCESS_KEY_ID": "fake-access-key",
                              "AWS_SECRET_ACCESS_KEY": "fake-secret-key"}):
        store = R2UploadStore(config, transport=transport)
        first = upload_checkpoint(snapshot, base_dir, store, checkpoint_hash,
                                  max_bytes=1024**2)
        assert first["blocks"] > 0 and first["uploaded_bytes"] > 0
        manifest_key = (f"/{config['bucket']}/{config['object_prefix']}/manifest.json")
        manifest_put = events.index(("PUT", manifest_key))
        assert all("/objects/" in path for method, path in events[:manifest_put]
                   if method == "PUT")
        assert all(any(previous == ("GET", path) for previous in events[:manifest_put])
                   for method, path in events[:manifest_put] if method == "PUT")
        second = upload_checkpoint(snapshot, base_dir, store, checkpoint_hash,
                                   max_bytes=1024**2)
        assert second["uploaded_bytes"] == 0 and second["reused_bytes"] > 0
        reader = R2ReadOnly(config, checkpoint_hash, transport=read_transport)
        destination = root / "remote-restored-overlay.sqlite"
        restored = restore_remote_checkpoint(base_dir, destination, reader,
                                             checkpoint_hash, max_bytes=1024**2)
        assert restored["restored_blocks"] == first["blocks"]
        source = CloudDisk(base_dir, source_overlay)
        target = CloudDisk(base_dir, destination)
        try:
            assert source.read(0, expected_bytes) == target.read(0, expected_bytes)
        finally:
            source.close()
            target.close()
        object_key = next(key for key in objects if "/objects/" in key)
        original = objects[object_key]
        objects[object_key] = b"BAD"
        failed = root / "remote-corrupt-restored-overlay.sqlite"
        try:
            restore_remote_checkpoint(base_dir, failed, reader, checkpoint_hash,
                                      max_bytes=1024**2)
            raise AssertionError("remote checkpoint accepted corrupt object")
        except ValueError:
            pass
        assert not failed.exists()
        objects[object_key] = original
        wrong_prefix = dict(config, object_prefix="disks/overlay-test/checkpoints/" + "0" * 64)
        try:
            upload_checkpoint(snapshot, base_dir,
                              R2UploadStore(wrong_prefix, transport=transport),
                              checkpoint_hash, max_bytes=1024**2)
            raise AssertionError("remote checkpoint accepted wrong object prefix")
        except ValueError:
            pass
    return True


def self_test_standalone():
    """Small fake-R2 checkpoint test when the full disk lab lacks host reserve."""
    from cloud_disk import pack
    with tempfile.TemporaryDirectory(prefix="vos-checkpoint-test-", dir="/private/tmp") as directory:
        root = Path(directory)
        source = root / "small.raw"
        source.write_bytes(bytes(BLOCK * 4))
        base = root / "base"
        pack(source, base)
        overlay = root / "overlay" / "blocks.sqlite"
        disk = CloudDisk(base, overlay, min_host_free_bytes=0)
        disk.write(133, b"acknowledged-fua" * 333)
        disk.flush()
        disk.close()
        checkpoint_dir = root / "checkpoint"
        checkpoint = snapshot_overlay(base, overlay, checkpoint_dir, max_bytes=1024**2)
        assert self_test_remote(root, base, overlay, checkpoint_dir,
                                checkpoint["checkpoint_sha256"], source.stat().st_size)
        print(json.dumps({"remote_overlay_checkpoint_fake_r2": "passed",
                          "acknowledged_flush_preserved": True,
                          "manifest_last": True,
                          "readback_verified": True,
                          "corruption_refused": True,
                          "wrong_prefix_refused": True}))


if __name__ == "__main__":
    if os.sys.argv[1:] != ["self-test"]:
        raise SystemExit("usage: cloud_disk_checkpoint.py self-test (use computer-space.sh)")
    self_test_standalone()
