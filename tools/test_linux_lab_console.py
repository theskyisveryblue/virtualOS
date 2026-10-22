import base64
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from linux_lab_console import send


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.fifo = Path(self.temp.name) / 'console'
        os.mkfifo(self.fifo, 0o600)
        self.payload = base64.b64encode(b'printf "CONFIRMED\\n"\n').decode()

    def test_absent_reader_times_out_instead_of_hanging(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            send(self.fifo, os.getpid(), self.payload, timeout=.05)
        self.assertLess(time.monotonic() - started, 1)

    def test_dead_guest_is_never_written(self):
        child = subprocess.Popen(['true'])
        child.wait()
        with self.assertRaises(ProcessLookupError):
            send(self.fifo, child.pid, self.payload)

    def test_command_echo_cannot_fake_guest_success(self):
        reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        send(self.fifo, os.getpid(), self.payload)
        command = os.read(reader, 4096)
        self.assertNotIn(b'CONFIRMED', command)
        actual = subprocess.check_output(['sh', '-c', command.decode()])
        self.assertEqual(actual, b'CONFIRMED\n')

    def test_failed_install_cannot_print_success_marker(self):
        reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        payload = base64.b64encode(b'set -e\nfalse\nprintf "CONFIRMED\\n"\n').decode()
        send(self.fifo, os.getpid(), payload)
        command = os.read(reader, 4096)
        result = subprocess.run(['sh', '-c', command.decode()], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'CONFIRMED', result.stdout)

    def test_symlink_and_public_fifo_are_rejected(self):
        alias = self.fifo.with_name('alias')
        alias.symlink_to(self.fifo)
        with self.assertRaises(ValueError):
            send(alias, os.getpid(), self.payload)
        self.fifo.chmod(0o644)
        with self.assertRaises(ValueError):
            send(self.fifo, os.getpid(), self.payload)


if __name__ == '__main__':
    unittest.main()
