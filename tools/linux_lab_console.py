"""Bounded writes to the private console FIFO of a disposable Linux guest."""
import base64
import errno
import os
from pathlib import Path
import stat
import sys
import time


def send(fifo, pid, payload, *, timeout=5):
    base64.b64decode(payload, validate=True)
    command = ("printf '%s' '" + payload + "' | base64 -d | sh\n").encode('ascii')
    if len(command) > 3072 or pid <= 1:
        raise ValueError('invalid guest console command')
    info = fifo.lstat()
    if (not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError('guest console FIFO must remain private')
    deadline = time.monotonic() + timeout
    fd = None
    try:
        while time.monotonic() < deadline:
            os.kill(pid, 0)
            try:
                if fd is None:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                    opened = os.fstat(fd)
                    if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
                        raise ValueError('guest console FIFO changed')
                written = os.write(fd, command)
                command = command[written:]
                if not command:
                    return
            except OSError as error:
                if error.errno not in (errno.ENXIO, errno.EAGAIN):
                    raise
            time.sleep(0.02)
        raise TimeoutError('guest console unavailable')
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == '__main__':
    try:
        send(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3])
    except (OSError, ValueError, TimeoutError) as error:
        raise SystemExit(f'linux_console=refused: {type(error).__name__}')
