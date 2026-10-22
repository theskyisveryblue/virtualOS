#!/usr/bin/env python3
"""Bounded real-R2 delta round trip using synthetic data, never a VM disk."""

import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid

from cloud_disk import BLOCK, digest, validate_config
from cloud_disk_r2_upload import pull_delta, upload_delta


def probe(config_path):
    config = validate_config(config_path)
    # Each run is isolated from existing immutable machine manifests.
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    config["object_prefix"] = "fleet/delta-probe-%s-%s" % (stamp, uuid.uuid4().hex[:12])
    # Three blocks cover zero data, a compressible pattern, and varied bytes.
    varied = b"".join(bytes.fromhex(digest(str(i).encode())) for i in range(BLOCK // 32))
    blocks = {0: bytes(BLOCK), 3: b"\xab" * BLOCK, 5: varied}
    with tempfile.TemporaryDirectory(prefix="vos-delta-probe-") as directory:
        source = Path(directory) / "source.sqlite"
        destination = Path(directory) / "pulled.sqlite"
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE blocks (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
            db.executemany("INSERT INTO blocks VALUES (?, ?)", blocks.items())
        first = upload_delta(source, config, base_label="synthetic-probe")
        pull_delta(config, destination, first["delta_manifest_sha256"])
        with sqlite3.connect(destination) as db:
            if dict(db.execute("SELECT idx, data FROM blocks")) != blocks:
                raise ValueError("real R2 delta round trip differs from source")
        again = upload_delta(source, config, base_label="synthetic-probe")
        if again["uploaded_bytes"] != 0 or again["reused_bytes"] != first["uploaded_bytes"]:
            raise ValueError("real R2 conditional writes did not reuse delta objects")
        try:
            pull_delta(config, destination, "0" * 64)
        except ValueError as error:
            if str(error) != "delta manifest does not match pinned SHA-256":
                raise
        else:
            raise ValueError("real R2 accepted an incorrect manifest pin")
        with sqlite3.connect(destination) as db:
            if dict(db.execute("SELECT idx, data FROM blocks")) != blocks:
                raise ValueError("failed pin check changed the verified local delta")
    return {
        "r2_delta_probe": "passed", "scope": "synthetic blocks; no VM boot or archive",
        "account_id": config["account_id"], "bucket": config["bucket"],
        "object_prefix": config["object_prefix"], "block_count": len(blocks),
        "raw_bytes": len(blocks) * BLOCK,
        "encoded_bytes": first["uploaded_bytes"],
        "delta_manifest_sha256": first["delta_manifest_sha256"],
        "readback_verified": True, "conditional_reuse_verified": True,
        "wrong_pin_preserves_destination": True,
        "remote_objects_retained": len(blocks) + 1,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: cloud_disk_delta_probe.py CONFIG (credentials in environment)")
    print(json.dumps(probe(sys.argv[1]), sort_keys=True))
