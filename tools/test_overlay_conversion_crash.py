"""Kill real conversion subprocesses and prove the source stays usable."""
import hashlib
from pathlib import Path
import subprocess
import sys
import time
import unittest

from compressed_delta import CompressedDeltaDisk
from compressed_write_overlay import convert_copy
from cloud_disk import BLOCK
import test_compressed_write_overlay as fixtures


CHILD = '''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import compressed_write_overlay as codec
base,delta,source,destination,marker=map(Path,sys.argv[2:7])
pin,phase=sys.argv[7:9]
def pause():
    marker.write_text('conversion-paused')
    while True:time.sleep(30)
if phase=='during_conversion':
    original=codec.CompressedWriteDisk._ensure_host_space
    def ensure(*args,**kwargs):
        original(*args,**kwargs)
        pause()
    codec.CompressedWriteDisk._ensure_host_space=ensure
else:
    codec.os.link=lambda *args,**kwargs:pause()
codec.convert_copy(base,delta,pin,source,destination,reserve=0,probe=lambda path:True)
'''


class ConversionCrashTests(unittest.TestCase):
    def check_interruption(self, phase):
        fixture = fixtures.CompressedWriteTests('test_conversion_and_reverse_keep_source_and_logical_fingerprint')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.raw_overlay()
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        destination = fixture.root / 'after-crash.sqlite'
        marker = fixture.root / 'paused'
        command = [sys.executable, '-c', CHILD, str(Path(__file__).resolve().parent),
                   str(fixture.base), str(fixture.delta), str(source), str(destination),
                   str(marker), fixture.pin, phase]
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists():
                if child.poll() is not None:
                    self.fail(child.communicate(timeout=2)[1].decode())
                if time.monotonic() > deadline:
                    self.fail('conversion did not reach interruption point')
                time.sleep(0.02)
            child.kill()
            child.communicate(timeout=5)
            self.assertLess(child.returncode, 0)
            self.assertFalse(destination.exists())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            # SIGKILL released the source lock; retry must work without editing it.
            result = convert_copy(fixture.base, fixture.delta, fixture.pin, source,
                                  destination, reserve=0, probe=lambda path: True)
            self.assertTrue(result['source_unchanged'])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            original = CompressedDeltaDisk(fixture.base, fixture.delta, source, fixture.pin,
                                          min_host_free_bytes=0)
            try:
                self.assertEqual(original.read(0, 2 * BLOCK), b'x' * (2 * BLOCK))
            finally:
                original.close()
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_sigkill_during_conversion_preserves_source_and_allows_retry(self):
        self.check_interruption('during_conversion')

    def test_sigkill_before_publication_preserves_source_and_allows_retry(self):
        self.check_interruption('before_publication')


if __name__ == '__main__':
    unittest.main()
