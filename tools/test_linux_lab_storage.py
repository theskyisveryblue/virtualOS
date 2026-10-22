import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from linux_lab_storage import discard, finalize, initialize, sparsify, write_state, SCHEMA


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.parent=Path(self.temp.name)
        self.source=self.parent/'source.img'; self.source.write_bytes(b'source')

    def run_directory(self,number):
        output=self.parent/('run.%08d'%number)
        output.mkdir(mode=0o700)
        initialize(output,self.source,self.parent)
        disk=output/'root.img';disk.write_bytes(b'private disposable disk');disk.chmod(0o600)
        (output/'install-console.log').write_text('retained evidence')
        return output,disk

    def test_resource_abort_removes_only_unused_private_disk_and_keeps_logs(self):
        output,disk=self.run_directory(1)
        result=finalize(output,self.parent,75,probe=lambda path:True)
        self.assertFalse(disk.exists())
        self.assertTrue((output/'install-console.log').exists())
        self.assertEqual(self.source.read_bytes(),b'source')
        self.assertEqual(result['removed_private_disks'],1)

    def test_verified_disk_is_never_eligible(self):
        output,disk=self.run_directory(1)
        finalize(output,self.parent,0,probe=lambda path:True)
        with self.assertRaises(ValueError):discard(output,self.parent,probe=lambda path:True)
        self.assertTrue(disk.exists())

    def test_open_or_unknown_disk_is_retained_and_refused(self):
        output,disk=self.run_directory(1)
        with self.assertRaisesRegex(ValueError,'open-file'):
            finalize(output,self.parent,75,probe=lambda path:False)
        self.assertTrue(disk.exists())

    def test_links_and_broad_permissions_are_refused(self):
        for case in ('symlink','hardlink','permissions'):
            with self.subTest(case=case):
                output,disk=self.run_directory(len(list(self.parent.glob('run.*')))+1)
                if case=='symlink':disk.unlink();disk.symlink_to(self.source)
                elif case=='hardlink':disk.unlink();os.link(self.source,disk);self.source.chmod(0o600)
                else:disk.chmod(0o644)
                with self.assertRaises(ValueError):finalize(output,self.parent,75,probe=lambda path:True)
                self.assertEqual(self.source.read_bytes(),b'source')

    def test_disk_swap_during_open_file_probe_is_refused(self):
        output,disk=self.run_directory(1)
        def replaced(path):
            path.unlink();path.symlink_to(self.source)
            return True
        with self.assertRaisesRegex(ValueError,'changed'):
            finalize(output,self.parent,75,probe=replaced)
        self.assertTrue(disk.is_symlink())

    def test_only_one_labelled_failed_disk_retained_and_unknown_runs_untouched(self):
        old,disk1=self.run_directory(1)
        finalize(old,self.parent,1,probe=lambda path:True)
        new,disk2=self.run_directory(2)
        unknown=self.parent/'run.unknown1';unknown.mkdir(mode=0o700)
        (unknown/'root.img').write_bytes(b'unlabelled')
        finalize(new,self.parent,1,probe=lambda path:True)
        self.assertFalse(disk1.exists())
        self.assertTrue(disk2.exists())
        self.assertTrue((unknown/'root.img').exists())

    def sparse_fixture(self, data):
        output,disk=self.run_directory(1)
        disk.write_bytes(data)
        write_state(output,dict(schema=SCHEMA,source=str(self.source),state='failed'))
        return output,disk,hashlib.sha256(data).hexdigest()

    def test_zero_image_becomes_sparse_without_changing_bytes_or_logs(self):
        data=bytes(2*1024**2)
        output,disk,pin=self.sparse_fixture(data)
        result=sparsify(output,self.parent,pin,probe=lambda p:True)
        self.assertTrue(result['sparsified'])
        self.assertGreater(result['allocated_bytes_reclaimed'],1024**2)
        self.assertEqual(disk.read_bytes(),data)
        self.assertEqual((output/'install-console.log').read_text(),'retained evidence')

    def test_nonzero_chunks_and_tail_are_preserved(self):
        data=bytes(65536)+b'x'*65536+bytes(1024**2)+b'tail'
        output,disk,pin=self.sparse_fixture(data)
        self.assertTrue(sparsify(output,self.parent,pin,probe=lambda p:True)['sparsified'])
        self.assertEqual(disk.read_bytes(),data)

    def test_wrong_hash_active_and_verified_images_are_preserved(self):
        output,disk,pin=self.sparse_fixture(bytes(1024**2))
        before=disk.stat().st_ino
        for checksum,probe in [('0'*64,lambda p:True),(pin,lambda p:False)]:
            with self.assertRaises(ValueError):sparsify(output,self.parent,checksum,probe=probe)
            self.assertEqual(disk.stat().st_ino,before)
        write_state(output,dict(schema=SCHEMA,source=str(self.source),state='verified'))
        with self.assertRaises(ValueError):sparsify(output,self.parent,pin,probe=lambda p:True)
        self.assertEqual(disk.stat().st_ino,before)


if __name__=='__main__':unittest.main()
