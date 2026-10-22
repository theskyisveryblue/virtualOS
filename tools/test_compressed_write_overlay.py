import hashlib
import fcntl
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
import zlib
from unittest.mock import patch
from types import SimpleNamespace

from cloud_disk import BLOCK, RawDisk
from compressed_delta import build, CompressedDeltaDisk
from compressed_write_overlay import CompressedWriteDisk, convert_copy, encode, decode


class CompressedWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'base.raw'
        self.machine = self.root / 'machine.raw'
        self.expected = b'b' * (3 * BLOCK) + b'c' * 512
        self.base.write_bytes(b'a' * len(self.expected))
        self.machine.write_bytes(self.expected)
        self.delta = self.root / 'delta.sqlite'
        self.pin = build(self.base, self.machine, self.delta, reserve=0)['delta_sha256']
        self.overlay = self.root / 'overlay.sqlite'

    def open(self, **kwargs):
        return CompressedWriteDisk(self.base, self.delta, self.overlay, self.pin,
                                   min_host_free_bytes=0, **kwargs)

    def test_codec_checksums_bounds_and_incompressible_fallback(self):
        for raw in (b'x', bytes(BLOCK), os.urandom(BLOCK), b'abcd' * 128):
            self.assertEqual(decode(encode(raw)), raw)
        self.assertEqual(encode(os.urandom(BLOCK))[36:37], b'R')
        self.assertLess(len(encode(bytes(BLOCK))), BLOCK // 100)
        valid = encode(b'x' * BLOCK)
        malformed = [b'', valid[:-1], valid + b'x', b'\0' * 38,
                     valid[:36] + b'?' + valid[37:],
                     struct.pack('>I', BLOCK + 1) + valid[4:]]
        # A small declared result must not permit a decompression bomb.
        malformed.append(struct.pack('>I', 1) + hashlib.sha256(b'x').digest() + b'Z' + zlib.compress(b'x' * BLOCK))
        for value in malformed:
            with self.assertRaises(ValueError):
                decode(value)

    def test_partial_writes_tail_and_restart_preserve_every_byte(self):
        disk = self.open()
        wanted = bytearray(self.expected)
        try:
            for off, data in ((BLOCK - 17, b'xyz' * 100), (3 * BLOCK + 400, b't' * 112)):
                disk.write(off, data)
                wanted[off:off + len(data)] = data
            disk.flush()
            self.assertEqual(disk.read(0, disk.size), wanted)
            raw = disk.db.execute('SELECT SUM(LENGTH(data)) FROM blocks').fetchone()[0]
            stored = disk.db.execute('SELECT SUM(LENGTH(data)) FROM encoded_blocks').fetchone()[0]
            self.assertLess(stored, raw // 10)
        finally:
            disk.close()
        disk = self.open()
        try:
            self.assertEqual(disk.read(0, disk.size), wanted)
        finally:
            disk.close()
        self.assertEqual(self.machine.read_bytes(), self.expected)

    def test_raw_and_old_layered_readers_refuse_new_format(self):
        disk = self.open()
        disk.write(0, b'x' * BLOCK)
        disk.close()
        with self.assertRaises(ValueError):
            RawDisk(self.base, self.overlay, min_host_free_bytes=0)
        with self.assertRaises(ValueError):
            CompressedDeltaDisk(self.base, self.delta, self.overlay, self.pin, min_host_free_bytes=0)

    def test_original_logical_cap_and_base_pruning_are_unchanged(self):
        disk = self.open(overlay_limit_bytes=BLOCK)
        try:
            disk.write(0, b'x' * BLOCK)
            with self.assertRaises(OSError):
                disk.write(0, b'y' * (2 * BLOCK))
            self.assertEqual(disk.read(0, BLOCK), b'x' * BLOCK)
            self.assertEqual(disk.read(BLOCK, BLOCK), b'b' * BLOCK)
            disk.write(0, b'z' * BLOCK)
            disk.write(0, b'b' * BLOCK)
            self.assertEqual(disk.db.execute('SELECT COUNT(*) FROM encoded_blocks').fetchone()[0], 0)
        finally:
            disk.close()

    def test_encoder_failure_rolls_back_whole_multi_block_write(self):
        disk = self.open()
        try:
            def fail_second(raw):
                if raw.startswith(b'y'):
                    raise ValueError('test failure')
                return encode(raw)
            disk.db.create_function('vos_overlay_encode_v1', 1, fail_second)
            with self.assertRaises(sqlite3.OperationalError):
                disk.write(0, b'x' * BLOCK + b'y' * BLOCK)
            self.assertEqual(disk.db.execute('SELECT COUNT(*) FROM encoded_blocks').fetchone()[0], 0)
            self.assertEqual(disk.read(0, disk.size), self.expected)
        finally:
            disk.close()

    def test_corruption_and_changed_schema_fail_closed(self):
        disk = self.open()
        disk.write(0, b'x' * BLOCK)
        disk.db.execute('UPDATE encoded_blocks SET data=?', (b'corrupt',))
        disk.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            disk.read(0, BLOCK)
        disk.db.execute('DROP TRIGGER blocks_insert')
        disk.close()
        with self.assertRaises(ValueError):
            self.open()

    def raw_overlay(self):
        source = self.root / 'raw.sqlite'
        disk = CompressedDeltaDisk(self.base, self.delta, source, self.pin, min_host_free_bytes=0)
        disk.write(0, b'x' * (2 * BLOCK))
        disk.close()
        return source

    def convert(self, source, destination, **kwargs):
        return convert_copy(self.base, self.delta, self.pin, source, destination,
                            reserve=0, probe=lambda p: True, **kwargs)

    def test_conversion_and_reverse_keep_source_and_logical_fingerprint(self):
        source = self.raw_overlay()
        original = source.read_bytes()
        compressed = self.root / 'compressed.sqlite'
        forward = self.convert(source, compressed)
        self.assertEqual(source.read_bytes(), original)
        self.assertLess(forward['destination_bytes'], forward['source_bytes'])
        restored = self.root / 'restored.sqlite'
        reverse = self.convert(compressed, restored, to_compressed=False)
        self.assertEqual(forward['logical_fingerprint'], reverse['logical_fingerprint'])
        disk = CompressedDeltaDisk(self.base, self.delta, restored, self.pin, min_host_free_bytes=0)
        try:
            self.assertEqual(disk.read(0, 2 * BLOCK), b'x' * (2 * BLOCK))
            self.assertEqual(disk.read(2 * BLOCK, BLOCK), b'b' * BLOCK)
        finally:
            disk.close()

    def test_conversion_refuses_open_pending_wrong_base_and_existing_destination(self):
        source = self.raw_overlay()
        dest = self.root / 'converted.sqlite'
        with self.assertRaises(ValueError):
            convert_copy(self.base, self.delta, self.pin, source, dest, reserve=0, probe=lambda p: False)
        wal = Path(str(source) + '-wal')
        wal.write_bytes(b'pending')
        with self.assertRaises(ValueError):
            self.convert(source, dest)
        wal.unlink()
        other = self.root / 'other.raw'
        other.write_bytes(self.base.read_bytes())
        with self.assertRaises(ValueError):
            convert_copy(other, self.delta, self.pin, source, dest, reserve=0, probe=lambda p: True)
        self.assertFalse(dest.exists())
        dest.write_bytes(b'preserved')
        with self.assertRaises(ValueError):
            self.convert(source, dest)
        self.assertEqual(dest.read_bytes(), b'preserved')

    def test_conversion_reserve_lock_and_source_change_fail_without_output(self):
        source = self.raw_overlay()
        dest = self.root / 'converted.sqlite'
        with patch('compressed_write_overlay.os.statvfs', return_value=SimpleNamespace(f_bavail=0, f_frsize=1)):
            with self.assertRaises(OSError):
                self.convert(source, dest)
        with source.with_suffix('.lock').open('r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.convert(source, dest)
        def mutate(path):
            if path == source:
                with source.open('ab') as target:
                    target.write(b'changed')
            return True
        with self.assertRaises(ValueError):
            convert_copy(self.base, self.delta, self.pin, source, dest, reserve=0, probe=mutate)
        self.assertFalse(dest.exists())
        self.assertFalse(list(self.root.glob('.write-overlay-*')))


if __name__ == '__main__':
    unittest.main()
