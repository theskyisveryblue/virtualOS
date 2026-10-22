import hashlib
from pathlib import Path
import tempfile
import unittest

from cloud_disk import BLOCK, RawDisk
from compressed_delta import build, CompressedDeltaDisk


class CompressedDeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.base=self.root/'base.raw';self.machine=self.root/'machine.raw'
        self.original=b'a'*BLOCK+b'b'*BLOCK+b'c'*512
        self.expected=b'a'*BLOCK+bytes(BLOCK)+b'd'*512
        self.base.write_bytes(self.original);self.machine.write_bytes(self.expected)
        self.delta=self.root/'delta.sqlite';self.overlay=self.root/'overlay.sqlite'
        self.receipt=build(self.base,self.machine,self.delta,reserve=0)

    def open(self,**limits):
        return CompressedDeltaDisk(self.base,self.delta,self.overlay,self.receipt['delta_sha256'],
                                   min_host_free_bytes=0,**limits)

    def test_seed_stays_compressed_and_is_never_copied_to_write_overlay(self):
        disk=self.open(overlay_limit_bytes=0)
        try:
            self.assertEqual(disk.read(0,len(self.expected)),self.expected)
            self.assertEqual(disk.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0],0)
            self.assertLess(self.receipt['stored_bytes'],self.receipt['raw_delta_bytes'])
        finally:disk.close()

    def test_runtime_changes_restore_to_layered_base_and_survive_restart(self):
        disk=self.open(overlay_limit_bytes=BLOCK)
        disk.write(BLOCK,b'x'*BLOCK);disk.close()
        disk=self.open(overlay_limit_bytes=BLOCK)
        try:
            self.assertEqual(disk.read(BLOCK,BLOCK),b'x'*BLOCK)
            disk.write(BLOCK,bytes(BLOCK))
            self.assertEqual(disk.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0],0)
            self.assertEqual(disk.read(0,len(self.expected)),self.expected)
        finally:disk.close()
        self.assertEqual(self.base.read_bytes(),self.original)
        self.assertEqual(self.machine.read_bytes(),self.expected)

    def test_plain_disk_refuses_layered_overlay_and_runtime_budget_stays_bounded(self):
        disk=self.open(overlay_limit_bytes=BLOCK)
        try:
            with self.assertRaises(OSError):disk.write(0,b'x'*(2*BLOCK))
            self.assertEqual(disk.read(0,len(self.expected)),self.expected)
        finally:disk.close()
        with self.assertRaises(ValueError):RawDisk(self.base,self.overlay,min_host_free_bytes=0)

    def test_wrong_pin_wrong_base_and_mutation_are_refused(self):
        with self.assertRaises(ValueError):CompressedDeltaDisk(self.base,self.delta,self.overlay,'0'*64)
        other=self.root/'other.raw';other.write_bytes(self.original)
        with self.assertRaises(ValueError):CompressedDeltaDisk(other,self.delta,self.overlay,self.receipt['delta_sha256'])
        disk=self.open()
        try:
            self.delta.chmod(0o600)
            with self.delta.open('ab') as stream:stream.write(b'changed')
            with self.assertRaises(OSError):disk.read(BLOCK,1)
        finally:disk.close()

    def test_existing_output_is_preserved_and_undersized_storage_budget_refuses(self):
        before=hashlib.sha256(self.delta.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):build(self.base,self.machine,self.delta,reserve=0)
        self.assertEqual(hashlib.sha256(self.delta.read_bytes()).hexdigest(),before)
        target=self.root/'too-small.sqlite'
        with self.assertRaises(OSError):build(self.base,self.machine,target,reserve=0,stored_limit=BLOCK)
        self.assertFalse(target.exists())

    def test_initial_delta_can_exceed_write_budget_without_using_it(self):
        base=self.root/'large-base.raw';base.write_bytes(b'a'*(8*BLOCK))
        machine=self.root/'large-machine.raw';machine.write_bytes(b'b'*(8*BLOCK))
        delta=self.root/'large-delta.sqlite';pin=build(base,machine,delta,reserve=0)
        self.assertGreater(pin['raw_delta_bytes'],BLOCK)
        disk=CompressedDeltaDisk(base,delta,self.overlay,pin['delta_sha256'],
                                 overlay_limit_bytes=BLOCK,min_host_free_bytes=0)
        try:
            self.assertEqual(disk.read(0,8*BLOCK),machine.read_bytes())
            self.assertEqual(disk.db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0],0)
        finally:disk.close()


if __name__=='__main__':unittest.main()
