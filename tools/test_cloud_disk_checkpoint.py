"""Checkpoint boundary checks without VM, network or production disk access."""
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock

from cloud_disk import (BLOCK, CloudDisk, pack, receive_exact, serve_nbd_connection,
                       NBD_MAGIC, NBD_OPTION_MAGIC, NBD_REQUEST_MAGIC,
                       NBD_SIMPLE_REPLY_MAGIC)
from cloud_disk_checkpoint import (snapshot_overlay, restore_overlay,
                                   restore_remote_checkpoint, _read_regular)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp', prefix='vos-undo-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'source.raw'
        source.write_bytes(bytes(BLOCK * 4))
        self.base = self.root / 'base'
        pack(source, self.base)

    def read_over_nbd(self, overlay):
        client, server = socket.socketpair()
        errors = []
        def serve():
            try:
                with server:
                    serve_nbd_connection(server, self.base, overlay,
                        disk_factory=lambda: CloudDisk(self.base, overlay, min_host_free_bytes=0))
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with client:
                client.settimeout(5)
                self.assertEqual(struct.unpack('>QQH', receive_exact(client, 18)),
                                 (NBD_MAGIC, NBD_OPTION_MAGIC, 1))
                export = b'virtualos-test'
                client.sendall(struct.pack('>I', 1) +
                    struct.pack('>QII', NBD_OPTION_MAGIC, 1, len(export)) + export)
                size, _ = struct.unpack('>QH', receive_exact(client, 10))
                receive_exact(client, 124)
                client.sendall(struct.pack('>IHHQQI', NBD_REQUEST_MAGIC, 0, 0, 17, 0, size))
                self.assertEqual(struct.unpack('>IIQ', receive_exact(client, 16)),
                                 (NBD_SIMPLE_REPLY_MAGIC, 0, 17))
                result = receive_exact(client, size)
                client.sendall(struct.pack('>IHHQQI', NBD_REQUEST_MAGIC, 0, 2, 18, 0, 0))
        finally:
            thread.join(timeout=6)
        self.assertFalse(thread.is_alive(), 'private NBD test did not exit')
        self.assertEqual(errors, [])
        return result

    def test_undo_restores_checkpoint_without_modifying_current_overlay(self):
        overlay = self.root / 'active' / 'overlay.sqlite'
        disk = CloudDisk(self.base, overlay, min_host_free_bytes=0)
        self.addCleanup(disk.close)
        disk.write(73, b'before agent task')
        disk.flush()
        before = disk.read(0, BLOCK * 4)
        snapshot = self.root / 'checkpoint'
        # Snapshot while the source SQLite connection is still active.
        pin = snapshot_overlay(self.base, overlay, snapshot)['checkpoint_sha256']
        disk.write(73, b'after agent task!')
        disk.write(BLOCK + 32, b'additional guest change')
        disk.flush()
        after = disk.read(0, BLOCK * 4)
        self.assertNotEqual(before, after)
        destination = self.root / 'restored.sqlite'
        restore_overlay(snapshot, self.base, destination, pin)
        restored = CloudDisk(self.base, destination, min_host_free_bytes=0)
        try:
            self.assertEqual(restored.read(0, BLOCK * 4), before)
            self.assertEqual(disk.read(0, BLOCK * 4), after)
            # Each connection opens a fresh disk service; reads must survive
            # disconnect/reopen and preserve the active overlay independently.
            self.assertEqual(self.read_over_nbd(destination), before)
            self.assertEqual(self.read_over_nbd(destination), before)
            self.assertEqual(self.read_over_nbd(overlay), after)
            with self.assertRaises(ValueError):
                restore_overlay(snapshot, self.base, overlay, pin)
        finally:
            restored.close()

    def test_fifo_is_refused_without_waiting_for_a_writer(self):
        path = self.root / 'fifo'
        os.mkfifo(path, 0o600)
        with self.assertRaises(ValueError):
            _read_regular(path, 128)

    def test_links_and_shared_writable_files_are_refused(self):
        path = self.root / 'data'
        path.write_bytes(b'checkpoint')
        path.chmod(0o600)
        link = self.root / 'symlink'
        link.symlink_to(path)
        with self.assertRaises(OSError):
            _read_regular(link, 128)
        hardlink = self.root / 'hardlink'
        os.link(path, hardlink)
        with self.assertRaises(ValueError):
            _read_regular(path, 128)
        hardlink.unlink()
        path.chmod(0o666)
        with self.assertRaises(ValueError):
            _read_regular(path, 128)

    def test_unpinned_remote_manifest_never_triggers_object_fetch(self):
        reader = Mock(prefix='disks/test/checkpoints/' + '0' * 64,
                      manifest_sha256='0' * 64)
        reader.fetch_manifest.return_value = b'{"blocks": []}'
        destination = self.root / 'untrusted.sqlite'
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            restore_remote_checkpoint(self.base, destination, reader, '0' * 64)
        reader.fetch_chunk.assert_not_called()
        self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
