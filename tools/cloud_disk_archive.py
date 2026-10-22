"""Convert a pinned VM tar.gz stream to R2 chunks without local disk extraction.

Objects are verified as they are uploaded. The boot manifest is published only
after both the compressed archive and extracted disk match their pinned hashes.
"""
import hashlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import re
import tarfile
import zlib

from cloud_disk import BLOCK, CHUNK, FORMAT, MAX_MANIFEST_BYTES, digest
from cloud_disk_r2_upload import MAX_RAW_BYTES


class PinnedArchiveReader:
    def __init__(self, source, expected_bytes):
        self.source = source
        self.expected_bytes = expected_bytes
        self.bytes_read = 0
        self.hasher = hashlib.sha256()

    def read(self, size):
        if not 0 <= size <= CHUNK:
            raise ValueError("archive reads must be bounded")
        data = self.source.read(size)
        if not isinstance(data, bytes) or len(data) > size:
            raise ValueError("invalid archive stream read")
        self.bytes_read += len(data)
        if self.bytes_read > self.expected_bytes:
            raise ValueError("archive exceeds pinned size")
        self.hasher.update(data)
        return data


def normalize_sparse_member(member, archive):
    """Restore GNU sparse logical size and preserve its physical tar boundary.

    bsdtar emits both PAX size (stored bytes) and GNU.sparse.realsize. Some
    Python versions apply size last, hiding the logical size and miscomputing
    the next header after the sparse map. No sparse extent is extracted locally.
    """
    if not member.issparse():
        return
    headers = member.pax_headers
    if headers.get("GNU.sparse.major") != "1" or headers.get("GNU.sparse.minor") != "0":
        raise ValueError("unsupported sparse archive format")
    try:
        logical = int(headers["GNU.sparse.realsize"])
    except (KeyError, ValueError):
        raise ValueError("invalid sparse logical size") from None
    if not 0 <= logical <= MAX_RAW_BYTES or len(member.sparse) > 65536:
        raise ValueError("sparse disk exceeds bounds")
    previous_end = stored = 0
    for offset, length in member.sparse:
        if (type(offset) is not int or type(length) is not int
                or offset < previous_end or length < 0 or offset + length > logical):
            raise ValueError("invalid sparse archive extent")
        previous_end = offset + length
        stored += length
    member.size = logical
    archive.offset = member.offset_data + (stored + 511) // 512 * 512


def stream_archive_upload(source, store, *, archive_bytes, archive_sha256,
                          disk_bytes, disk_sha256, workers=1, progress=None):
    """Read sequentially from a private archive stream; never extract a file."""
    for pin in (archive_sha256, disk_sha256):
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin):
            raise ValueError("archive and disk SHA-256 pins required")
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("archive upload workers must be 1..4")
    if (type(disk_bytes) is not int or not 512 <= disk_bytes <= MAX_RAW_BYTES
            or disk_bytes % 512 or type(archive_bytes) is not int
            or not 1 <= archive_bytes <= MAX_RAW_BYTES + 128 * 1024**2):
        raise ValueError("invalid pinned archive or disk size")
    reader = PinnedArchiveReader(source, archive_bytes)
    entries = []
    raw_hash = hashlib.sha256()
    uploaded_bytes = reused_bytes = metadata_bytes = members = 0
    found = False
    pending = deque()

    def upload(raw):
        size = len(raw)
        if raw.count(0) == size:
            return {"zero": True, "raw_size": size}, 0, 0
        encoded = zlib.compress(raw, level=1)
        key = digest(encoded)
        object_key = "objects/" + key + ".z"
        # Resume by reading verified immutable content, without sending the
        # same multi-megabyte PUT body over the Mini's uplink again.
        remote = store.get_if_present(object_key, CHUNK + 65536)
        new = False
        if remote is None:
            new = store.put_if_absent(object_key, encoded)
            remote = store.get(object_key, CHUNK + 65536)
        if len(remote) != len(encoded) or digest(remote) != key:
            if progress is not None:
                progress(dict(state="integrity_failure", object_key=object_key,
                              expected_bytes=len(encoded), received_bytes=len(remote),
                              received_sha256=digest(remote)))
            raise ValueError("archive chunk failed R2 readback")
        return ({"key": key, "sha256": digest(raw), "raw_size": size,
                 "encoded_size": len(encoded)}, len(encoded) if new else 0,
                0 if new else len(encoded))

    def collect():
        nonlocal uploaded_bytes, reused_bytes
        entry, uploaded, reused = pending.popleft().result()
        entries.append(entry)
        uploaded_bytes += uploaded
        reused_bytes += reused
        if progress is not None and (len(entries) % 16 == 0 or len(entries) * CHUNK >= disk_bytes):
            progress(dict(state="archive_conversion", verified_bytes=min(len(entries) * CHUNK, disk_bytes),
                          logical_bytes=disk_bytes, archive_bytes_read=reader.bytes_read,
                          uploaded_bytes=uploaded_bytes, reused_bytes=reused_bytes,
                          chunks_verified=len(entries), upload_workers=workers))

    with ThreadPoolExecutor(max_workers=workers) as pool, tarfile.open(fileobj=reader, mode="r|gz") as archive:
        for member in archive:
            normalize_sparse_member(member, archive)
            members += 1
            if members > 100:
                raise ValueError("too many archive members")
            # The existing backup is tar -C BUNDLE ., hence ./Disk.img.
            name = member.name.removeprefix("./")
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise ValueError("archive contains an unsupported member")
            if name != "Disk.img":
                metadata_bytes += member.size
                if metadata_bytes > 128 * 1024**2:
                    raise ValueError("archive metadata exceeds bound")
                continue
            if found or not member.isfile() or member.size != disk_bytes:
                raise ValueError("archive disk member does not match pinned shape")
            found = True
            disk = archive.extractfile(member)
            if disk is None:
                raise ValueError("archive disk is unreadable")
            with disk:
                for offset in range(0, disk_bytes, CHUNK):
                    size = min(CHUNK, disk_bytes - offset)
                    raw = disk.read(size)
                    if len(raw) != size:
                        raise ValueError("short archive disk read")
                    raw_hash.update(raw)
                    pending.append(pool.submit(upload, raw))
                    # Bound resident raw/compressed/readback chunks, including
                    # when one earlier upload is slower than later uploads.
                    if len(pending) >= workers:
                        collect()
                while pending:
                    collect()
    # tar may stop at its end marker before consuming the compressed stream.
    while reader.read(65536):
        pass
    if (not found or reader.bytes_read != archive_bytes
            or reader.hasher.hexdigest() != archive_sha256
            or raw_hash.hexdigest() != disk_sha256):
        raise ValueError("archive or disk does not match pinned content")
    manifest = {"format": FORMAT, "logical_size": disk_bytes,
                "chunk_size": CHUNK, "block_size": BLOCK, "chunks": entries}
    body = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError("archive disk manifest exceeds bound")
    store.put_if_absent("manifest.json", body)
    if store.get("manifest.json", len(body)) != body:
        raise ValueError("archive disk manifest conflicts with existing pin")
    return {"manifest_sha256": digest(body), "logical_bytes": disk_bytes,
            "archive_bytes": reader.bytes_read, "archive_sha256": archive_sha256,
            "disk_sha256": disk_sha256, "chunk_count": len(entries),
            "uploaded_bytes": uploaded_bytes, "reused_bytes": reused_bytes,
            "readback_verified": True, "local_disk_extracted": False}
