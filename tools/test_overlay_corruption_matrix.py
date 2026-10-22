"""Additional integrity checks for existing writes and valid-but-altered payloads."""
import os
import sqlite3
import unittest

from cloud_disk import BLOCK
from compressed_write_overlay import decode, encode
import test_compressed_write_overlay as fixtures


class CorruptionMatrix(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.CompressedWriteTests('test_codec_checksums_bounds_and_incompressible_fallback')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_valid_stream_with_wrong_checksum_or_payload_is_rejected(self):
        for raw in (b'x' * BLOCK, os.urandom(BLOCK)):
            with self.subTest(compressed=raw[:2] == b'xx'):
                encoded = encode(raw)
                checksum = bytearray(encoded)
                checksum[4] ^= 1
                with self.assertRaises(ValueError):
                    decode(bytes(checksum))
                # Both streams are structurally valid and the declared lengths match.
                changed = encode(bytes([raw[0] ^ 1]) + raw[1:])
                with self.assertRaises(ValueError):
                    decode(encoded[:36] + changed[36:])

    def test_unknown_persistent_codec_cannot_reopen(self):
        fixture = self.fixture()
        disk = fixture.open()
        disk.write(0, b'p' * BLOCK)
        disk.close()
        db = sqlite3.connect(fixture.overlay)
        try:
            db.execute("UPDATE metadata SET value='unknown-v9' WHERE name='overlay_codec'")
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, 'unsupported write overlay codec'):
            fixture.open()

    def test_multi_block_encoder_failure_preserves_prior_encoded_rows(self):
        fixture = self.fixture()
        disk = fixture.open()
        try:
            disk.write(0, b'p' * BLOCK)
            before = disk.db.execute('SELECT idx,data FROM encoded_blocks ORDER BY idx').fetchall()
            def fail_second(raw):
                if raw.startswith(b'y'):
                    raise ValueError('deliberate second-block failure')
                return encode(raw)
            disk.db.create_function('vos_overlay_encode_v1', 1, fail_second)
            with self.assertRaises(sqlite3.OperationalError):
                disk.write(0, b'x' * BLOCK + b'y' * BLOCK)
            self.assertEqual(disk.db.execute('SELECT idx,data FROM encoded_blocks ORDER BY idx').fetchall(), before)
            self.assertEqual(disk.read(0, 2 * BLOCK), b'p' * BLOCK + b'b' * BLOCK)
        finally:
            disk.close()


if __name__ == '__main__':
    unittest.main()
