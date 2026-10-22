import gzip
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from linux_prepare_ext4 import VZ_BOOT_MODULES, dependency_closure, entries, inflate, prepare, record, requested_modules, verify_package


class PreparationTests(unittest.TestCase):
    def test_newc_is_readable_by_independent_archive_tool(self):
        raw = record('etc', b'', 1, 0o40755)
        raw += record('etc/fixture', b'preserved\n', 2)
        raw += record('bin/sh', b'/bin/busybox', 3, 0o120777)
        raw += record('TRAILER!!!', b'', 4)
        parsed = list(entries(raw))
        self.assertEqual([row[0] for row in parsed], ['etc', 'etc/fixture', 'bin/sh'])
        self.assertEqual(b''.join(row[3] for row in parsed), raw[:-len(record('TRAILER!!!', b'', 4))])
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            path = Path(name) / 'initramfs'
            path.write_bytes(gzip.compress(raw))
            contents = subprocess.check_output(['tar', '-xOf', str(path), 'etc/fixture'])
        self.assertEqual(contents, b'preserved\n')

    def test_unsafe_and_incomplete_archives_are_rejected(self):
        for raw in (record('../outside', b'bad', 1), record('good', b'body', 1)[:-5]):
            with self.subTest(raw=raw[:6]), self.assertRaises(ValueError):
                list(entries(raw))

    def test_decompression_is_bounded(self):
        with self.assertRaises(ValueError):
            inflate(gzip.compress(b'a' * 50000), 1000)

    def test_dependencies_include_transitive_requirements_and_builtins(self):
        deps = {'ext4': ['jbd2', 'crc'], 'jbd2': ['crc']}
        self.assertEqual(dependency_closure({'ext4'}, deps, {'ext4', 'jbd2'}, {'crc'}), {'ext4', 'jbd2'})
        with self.assertRaises(ValueError):
            dependency_closure({'ext4'}, deps, {'ext4'}, {'crc'})

    def test_compressed_package_modules_match_uncompressed_initramfs_names(self):
        deps = {'ext4.ko.gz': ['jbd2.ko.gz'], 'jbd2.ko.gz': []}
        self.assertEqual(dependency_closure({'ext4.ko'}, deps, set(deps), set()), set(deps))

    def test_unsigned_package_cannot_create_artifacts(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            root = Path(name)
            package = root / 'package.apk'; package.write_bytes(b'untrusted')
            initrd = root / 'initrd'; initrd.write_bytes(b'fixture')
            output = root / 'output'
            with self.assertRaisesRegex(ValueError, 'signature-verified pin'):
                prepare(package, initrd, output)
            self.assertFalse(output.exists())

    def test_explicit_profile_cannot_omit_boot_drivers_or_escape_module_tree(self):
        self.assertEqual(requested_modules([],dict(required_modules=sorted(VZ_BOOT_MODULES))),VZ_BOOT_MODULES)
        for wanted in ([], sorted(VZ_BOOT_MODULES-{'kernel/fs/ext4/ext4.ko'}),
                       sorted(VZ_BOOT_MODULES)+['kernel/../../outside.ko']):
            with self.assertRaises(ValueError):
                requested_modules([],dict(required_modules=wanted))

    def test_package_signature_and_data_hash_are_independent_gates(self):
        def tar(name,body):
            output=io.BytesIO()
            with tarfile.open(fileobj=output,mode='w:') as archive:
                member=tarfile.TarInfo(name); member.size=len(body)
                archive.addfile(member,io.BytesIO(body))
            return output.getvalue()
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            root=Path(name)
            private,key=root/'private.pem',root/'key.pem'
            subprocess.run(['openssl','genrsa','-out',str(private),'2048'],check=True,capture_output=True)
            subprocess.run(['openssl','rsa','-in',str(private),'-pubout','-out',str(key)],check=True,capture_output=True)
            payload=gzip.compress(tar('fixture',b'authenticated payload'))
            metadata=('pkgname = linux-virt\npkgver = 6.18.57-r0\narch = aarch64\ndatahash = '
                      +hashlib.sha256(payload).hexdigest()+'\n').encode()
            control=gzip.compress(tar('.PKGINFO',metadata))
            signature=subprocess.run(['openssl','dgst','-sha1','-sign',str(private)],input=control,
                                     check=True,capture_output=True).stdout
            envelope=gzip.compress(tar('.SIGN.RSA.alpine-devel@lists.alpinelinux.org-616ae350.rsa.pub',signature))
            package=root/'fixture.apk'; package.write_bytes(envelope+control+payload)
            anchor=hashlib.sha256(key.read_bytes()).hexdigest()
            with patch('linux_prepare_ext4.ALPINE_KEY_SHA256',anchor):
                result=verify_package(package,key)
                self.assertTrue(result['signature_verified'])
                self.assertEqual(result['package'],'linux-virt')
                package.write_bytes(envelope+control+payload+b'tampered')
                with self.assertRaisesRegex(ValueError,'data differs'):
                    verify_package(package,key)
                changed=gzip.compress(tar('.PKGINFO',metadata.replace(b'6.18.57',b'6.18.58')))
                package.write_bytes(envelope+changed+payload)
                with self.assertRaisesRegex(ValueError,'signature verification failed'):
                    verify_package(package,key)
            with self.assertRaisesRegex(ValueError,'trusted anchor'):
                verify_package(package,key)


if __name__ == '__main__':
    unittest.main()
