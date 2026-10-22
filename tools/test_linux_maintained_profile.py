import gzip
from pathlib import Path
import stat
import struct
import tempfile
import unittest

from linux_maintained_profile import pack,resolve,safe_name,validate_binary
from linux_prepare_ext4 import entries,record


class MaintainedTests(unittest.TestCase):
    def test_device_nodes_keep_original_major_minor_and_records(self):
        raw=bytearray(record('dev/console',b'',12,stat.S_IFCHR|0o600))
        raw[6+9*8:6+10*8]=b'00000005'
        raw[6+10*8:6+11*8]=b'00000001'
        files={'dev':(stat.S_IFDIR|0o755,b'','structure'),
               'dev/console':(stat.S_IFCHR|0o600,b'','kernel-or-device-structure')}
        archive=gzip.decompress(pack(files,{'dev/console':bytes(raw)}))
        found={name:entry for name,mode,body,entry in entries(archive)}
        self.assertEqual(found['dev/console'],bytes(raw))

    def test_guest_symlinks_can_resolve_absolute_targets_but_cannot_escape(self):
        files={'bin':(stat.S_IFDIR,b'',''), 'lib':(stat.S_IFDIR,b'',''),
               'lib/loader':(stat.S_IFREG,b'loader',''),
               'bin/loader':(stat.S_IFLNK,b'/lib/loader','')}
        self.assertEqual(resolve(files,'bin/loader')[1],b'loader')
        files['bin/loader']=(stat.S_IFLNK,b'../../outside','')
        with self.assertRaises(ValueError):resolve(files,'bin/loader')
        files['bin/loader']=(stat.S_IFLNK,b'loader','')
        with self.assertRaisesRegex(ValueError,'loop'):resolve(files,'bin/loader')

    def test_elf_dependencies_require_matching_arch_and_existing_interpreter(self):
        data=bytearray(160)
        data[:6]=b'\x7fELF\x02\x01'
        struct.pack_into('<H',data,18,183)
        struct.pack_into('<Q',data,32,64)
        struct.pack_into('<HH',data,54,56,1)
        struct.pack_into('<I',data,64,3)
        struct.pack_into('<Q',data,72,120)
        interpreter=b'/lib/loader\0'
        struct.pack_into('<Q',data,96,len(interpreter))
        data[120:120+len(interpreter)]=interpreter
        files={'bin':(stat.S_IFDIR,b'',''),'lib':(stat.S_IFDIR,b'',''),
               'bin/program':(stat.S_IFREG|0o755,bytes(data),''),
               'lib/loader':(stat.S_IFREG,b'loader','')}
        validate_binary(files,'bin/program')
        del files['lib/loader']
        with self.assertRaises(ValueError):validate_binary(files,'bin/program')
        struct.pack_into('<H',data,18,62)
        files['bin/program']=(stat.S_IFREG|0o755,bytes(data),'')
        with self.assertRaisesRegex(ValueError,'architecture'):validate_binary(files,'bin/program')

    def test_archive_paths_never_escape(self):
        for path in ('../outside','/host','a/../../host',''):
            with self.assertRaises(ValueError):safe_name(path)


if __name__=='__main__':unittest.main()
