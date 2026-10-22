import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

import linux_guest_selection as subject


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.files={}
        for name,body in (('Image',b'kernel'),('initramfs',b'initrd'),
                          ('proof.img',b'root disk'),('runtime.img',b'root disk')):
            path=self.root/name;path.write_bytes(body);path.chmod(0o600)
            self.files[name]=path
        self.pins=dict(kernel_sha256=self.sha(self.files['Image']),
                       initramfs_sha256=self.sha(self.files['initramfs']),
                       rootfs_profile_sha256='a'*64)
        self.disk_sha=self.sha(self.files['proof.img'])
        self.profile=self.root/'profile.json';self.write(self.profile,self.pins)
        self.receipt=self.root/'receipt.json'
        self.write(self.receipt,dict(schema='virtualos-maintained-linux-persistence-receipt/v1',
            **self.pins,disk_rooted_boot_1=True,disk_rooted_boot_2=True,maintained_userland=True,
            root_login_locked=True,persistent=True,files={'root.img':{'sha256':self.disk_sha,'bytes':len(b'root disk')}}))
        self.config=self.root/'selection.json'
        self.row=dict(schema=subject.SCHEMA,mode='maintained',profile=str(self.profile),
            receipt=self.reference(self.receipt),proof_disk=str(self.files['proof.img']),
            runtime_disk=str(self.files['runtime.img']))
        for target,value in (('PINS',self.pins),('DISK_SHA256',self.disk_sha),('DISK_BYTES',len(b'root disk'))):
            mock=patch.object(subject,target,value);mock.start();self.addCleanup(mock.stop)
        mock=patch.object(subject,'check',return_value=self.pins)
        mock.start();self.addCleanup(mock.stop)

    @staticmethod
    def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

    def reference(self,path):return {'path':str(path),'sha256':self.sha(path)}

    @staticmethod
    def write(path,value):
        path.write_text(json.dumps(value));path.chmod(0o600)

    def run_selection(self):
        self.write(self.config,self.row)
        return subject.selection(self.config)

    def test_valid_maintained_selection_only_prepares_separate_runtime(self):
        result=self.run_selection()
        self.assertEqual(result['mode'],'maintained')
        self.assertTrue(result['activation_required'])
        self.assertFalse(result['default_selection_changed'])
        self.assertFalse(result['disk_mutation_performed'])
        self.assertIn(str(self.files['runtime.img']),result['arguments'])
        self.assertNotIn(str(self.files['proof.img']),result['arguments'])

    def test_unknown_and_missing_modes_have_no_legacy_fallback(self):
        for mode in (None,'auto','unsupported'):
            self.row['mode']=mode
            with self.assertRaisesRegex(ValueError,'explicitly'):self.run_selection()

    def test_legacy_requires_explicit_pinned_artifacts(self):
        self.row=dict(schema=subject.SCHEMA,mode='legacy',
            kernel=self.reference(self.files['Image']),initramfs=self.reference(self.files['initramfs']),
            disk=self.reference(self.files['runtime.img']))
        self.assertEqual(self.run_selection()['mode'],'legacy')
        self.row['kernel']['sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'pin mismatch'):self.run_selection()

    def test_receipt_or_profile_tampering_refuses_instead_of_fallback(self):
        self.row['receipt']['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'pin mismatch'):self.run_selection()
        self.row['receipt']=self.reference(self.receipt)
        with patch.object(subject,'check',return_value=dict(self.pins,kernel_sha256='0'*64)):
            with self.assertRaisesRegex(ValueError,'two-boot pins'):self.run_selection()

    def test_malformed_receipt_refuses_with_a_policy_reason(self):
        receipt=json.loads(self.receipt.read_text());receipt['files']=[]
        self.write(self.receipt,receipt);self.row['receipt']=self.reference(self.receipt)
        with self.assertRaisesRegex(ValueError,'proof receipt is incomplete'):self.run_selection()

    def test_existing_runtime_writes_require_separate_lifecycle_review(self):
        self.files['runtime.img'].write_bytes(b'changed root')
        with self.assertRaisesRegex(ValueError,'sealed maintained proof pin'):self.run_selection()

    def test_proof_disk_cannot_be_the_runtime_disk(self):
        self.row['runtime_disk']=self.row['proof_disk']
        with self.assertRaisesRegex(ValueError,'separate private copy'):self.run_selection()

    def test_config_symlinks_hardlinks_and_broad_permissions_refused(self):
        self.write(self.config,self.row)
        linked=self.root/'linked.json';linked.symlink_to(self.config)
        with self.assertRaises((OSError,ValueError)):subject.selection(linked)
        linked.unlink();os.link(self.config,linked)
        with self.assertRaises(ValueError):subject.selection(self.config)
        linked.unlink();self.config.chmod(0o644)
        with self.assertRaises(ValueError):subject.selection(self.config)

    def test_runtime_symlinks_hardlinks_and_broad_permissions_refused(self):
        runtime=self.files['runtime.img'];runtime.unlink();runtime.symlink_to(self.files['proof.img'])
        with self.assertRaises((OSError,ValueError)):self.run_selection()
        runtime.unlink();os.link(self.files['proof.img'],runtime)
        with self.assertRaises(ValueError):self.run_selection()
        runtime.unlink();runtime.write_bytes(b'root disk');runtime.chmod(0o644)
        with self.assertRaises(ValueError):self.run_selection()

    def test_duplicate_fields_and_unprotected_parent_refused(self):
        self.config.write_text('{"schema":"first","schema":"second"}');self.config.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'duplicate'):subject.selection(self.config)
        self.write(self.config,self.row);self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError,'parent must be private'):subject.selection(self.config)

    def test_growing_input_is_bounded_to_initial_size_plus_one(self):
        info=SimpleNamespace(st_size=8,st_mtime_ns=1,st_ctime_ns=1)
        stream=Mock();reads=[]
        def growing_read(size):reads.append(size);return b'x'*size
        stream.read.side_effect=growing_read
        with patch.object(subject,'owned_file') as opened:
            opened.return_value.__enter__.return_value=(stream,info)
            with self.assertRaisesRegex(ValueError,'grew'):subject.file_hash(self.files['Image'])
        self.assertEqual(reads,[8,1])

    def test_shortened_input_is_refused_before_hash_is_used(self):
        stream=Mock();stream.read.side_effect=[b'abc',b'']
        with patch.object(subject,'owned_file') as opened:
            opened.return_value.__enter__.return_value=(stream,SimpleNamespace(st_size=8))
            with self.assertRaisesRegex(ValueError,'shortened'):subject.file_hash(self.files['Image'])


if __name__=='__main__':unittest.main()
