"""Disposable fake-S3-to-NBD bridge for the existing 16 MiB VZ lab.

Invoked only by computer-space.sh; all network listeners bind loopback.
"""

import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

from cloud_disk import (BLOCK, CHUNK, CLOUD_DISK_BUCKET, CONFIG_FORMAT, digest,
                        lab_path, open_r2_disk, serve_nbd_connection)
from cloud_disk_r2 import self_test as fake_s3_self_test


def main():
    if len(sys.argv) != 5:
        raise SystemExit("usage: cloud_disk_vz_fake.py PACKED_ROOT OVERLAY CACHE PORT")
    directory, overlay, cache = map(lab_path, sys.argv[1:4])
    port = int(sys.argv[4])
    if not 20000 <= port <= 60999 or not directory.is_dir():
        raise ValueError("disposable packed disk and lab port required")
    manifest = json.loads((directory / "manifest.json").read_text())
    if not 512 <= manifest.get("logical_size", 0) <= 16 * 1024**2:
        raise ValueError("fake R2 VZ export exceeds 16 MiB")
    manifest_hash = digest((directory / "manifest.json").read_bytes())
    account = "f" * 32
    config = {"schema": CONFIG_FORMAT, "account_id": account,
              "bucket": CLOUD_DISK_BUCKET, "endpoint": f"https://{account}.r2.cloudflarestorage.com",
              "object_prefix": "disks/test", "storage_class": "Standard",
              "chunk_size_bytes": CHUNK, "block_size_bytes": BLOCK,
              "local_cache_limit_bytes": CHUNK, "local_overlay_limit_bytes": CHUNK,
              "boot_cache_required": True, "credentials": "environment_only"}
    config_path = directory.parent / "fake-r2-config.json"
    config_path.write_text(json.dumps(config))

    served = []

    def serve(fake_transport):
        # self_test() invokes the open_disk hook twice to check cache reuse on
        # reopen. Serving a second blocking accept() would hang forever (the VZ
        # client connects only once), so only the first call serves NBD.
        if served:
            return
        served.append(True)
        factory = lambda: open_r2_disk(config_path, manifest_hash, overlay, cache,
                                       min_host_free_bytes=0, transport=fake_transport)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            listener.settimeout(120)
            print(json.dumps({"fake_r2_vz_listener": "ready", "host": "127.0.0.1",
                              "port": port, "remote_account": "fake", "writes_to_r2": 0}), flush=True)
            connection, _ = listener.accept()
            stats = {}
            serve_nbd_connection(connection, None, overlay, stats=stats,
                                 disk_factory=factory, idle_timeout=15)
            print(json.dumps({"fake_r2_vz_connection": stats}, sort_keys=True), flush=True)

    with patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": account,
                              "CLOUDFLARE_R2_ENDPOINT": config["endpoint"],
                              "AWS_ACCESS_KEY_ID": "fake-access-key",
                              "AWS_SECRET_ACCESS_KEY": "fake-secret-key"}):
        fake_s3_self_test(directory, config, manifest_hash, open_disk=serve)
    print(json.dumps({"fake_r2_vz_server": "passed", "signed_gets": True,
                      "corruption_rejected": True}), flush=True)


if __name__ == "__main__":
    main()
