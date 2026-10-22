from pathlib import Path
import tempfile
import unittest
import zlib

from disk_footprint import BLOCK, measure


class FootprintTests(unittest.TestCase):
    def test_exact_delta_counts_include_zero_over_nonzero_and_partial_tail(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            base=Path(directory)/'base.raw'; machine=Path(directory)/'machine.raw'
            base.write_bytes(b'a'*BLOCK+b'b'*BLOCK+b'c'*512)
            machine.write_bytes(b'a'*BLOCK+bytes(BLOCK)+b'd'*512)
            result=measure(base,machine,reserve=0)
            self.assertEqual(result['differing_blocks'],2)
            self.assertEqual(result['delta_raw_bytes'],BLOCK+512)
            self.assertEqual(result['changed_to_zero_blocks'],1)
            self.assertEqual(result['delta_zlib_level1_payload_bytes'],
                             len(zlib.compress(bytes(BLOCK),1))+len(zlib.compress(b'd'*512,1)))
            self.assertFalse(result['actual_unique_apfs_extents_measured'])

    def test_identical_disks_have_no_delta_and_mismatched_sizes_are_refused(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as directory:
            base=Path(directory)/'base.raw'; machine=Path(directory)/'machine.raw'
            base.write_bytes(bytes(BLOCK)); machine.write_bytes(bytes(BLOCK))
            self.assertEqual(measure(base,machine,reserve=0)['delta_raw_bytes'],0)
            machine.write_bytes(bytes(512))
            with self.assertRaises(ValueError):measure(base,machine,reserve=0)


if __name__=='__main__':unittest.main()
