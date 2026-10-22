import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from linux_boot_artifacts import audit


class BootArtifactsTests(unittest.TestCase):
    def fixture(self, files, release='6.6.134-0-virt'):
        temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        kernel = root / 'Image'
        kernel.write_bytes(b'fixture\0Linux version 6.6.134-0-virt build\0')
        initrd = root / 'initramfs'
        with tarfile.open(initrd, 'w:gz') as archive:
            for name, body in files.items():
                header = tarfile.TarInfo('lib/modules/' + release + '/' + name)
                header.size = len(body)
                archive.addfile(header, io.BytesIO(body))
        return kernel, initrd

    def test_missing_ext4_is_refused_before_a_guest_can_start(self):
        files = {'modules.dep': b'kernel/fs/squashfs/squashfs.ko:\n',
                 'kernel/fs/squashfs/squashfs.ko': b'fixture'}
        self.assertEqual(audit(*self.fixture(files))['reason'], 'ext4_driver_missing')

    def test_driver_from_another_kernel_is_refused(self):
        self.assertEqual(audit(*self.fixture({'modules.dep': b''}, release='6.6.142-0-virt'))['reason'],
                         'kernel_module_release_mismatch')

    def test_all_transitive_dependencies_must_exist(self):
        files = {'modules.dep': b'kernel/fs/ext4/ext4.ko: kernel/fs/jbd2/jbd2.ko\nkernel/fs/jbd2/jbd2.ko:\n',
                 'kernel/fs/ext4/ext4.ko': b'fixture'}
        result = audit(*self.fixture(files))
        self.assertEqual(result['reason'], 'ext4_dependency_missing')
        self.assertEqual(result['missing'], ['kernel/fs/jbd2/jbd2.ko'])

    def test_matching_complete_driver_is_ready_but_does_not_claim_boot(self):
        files = {'modules.dep': b'kernel/fs/ext4/ext4.ko: kernel/fs/jbd2/jbd2.ko\nkernel/fs/jbd2/jbd2.ko:\n',
                 'kernel/fs/ext4/ext4.ko': b'ext4', 'kernel/fs/jbd2/jbd2.ko': b'jbd2'}
        result = audit(*self.fixture(files))
        self.assertEqual(result['linux_artifacts'], 'ready')
        self.assertFalse(result['boot_verified'])

    def test_compiled_in_ext4_does_not_require_a_separate_module(self):
        files = {'modules.builtin': b'kernel/fs/ext4/ext4.ko\n'}
        self.assertEqual(audit(*self.fixture(files))['linux_artifacts'], 'ready')


if __name__ == '__main__':
    unittest.main()
