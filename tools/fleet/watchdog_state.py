"""Private, locked rate state for fleet recovery; never evaluates state as code."""
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time


def guest_admission(report):
    """Admission for a 4 GiB / 2 CPU macOS VM, not a single guest input."""
    if not isinstance(report, dict):
        return False
    integers = ('disk_free_kib', 'memory_free_percent', 'swap_used_mib', 'cpu_count')
    if any(type(report.get(key)) is not int for key in integers):
        return False
    load = report.get('load_one')
    if type(load) not in (int, float):
        return False
    try:
        if not math.isfinite(load):
            return False
    except OverflowError:
        return False
    return (report['disk_free_kib'] >= 14 * 1024**2
            and 50 <= report['memory_free_percent'] <= 100
            and 0 <= report['swap_used_mib'] < 8 * 1024
            and report['cpu_count'] >= 2 and 0 <= load < report['cpu_count'])


def unique_fields(pairs):
    report = {}
    for key, value in pairs:
        if key in report:
            raise ValueError('duplicate watchdog resource field')
        report[key] = value
    return report


def guest_admission_report(stream):
    body = stream.read(65537)
    if len(body) > 65536:
        raise ValueError('watchdog resource report exceeds bound')
    return guest_admission(json.loads(body, object_pairs_hook=unique_fields))


def checked(info, directory=False):
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise ValueError('watchdog state must be private and owned')


def admit(home, maximum, now=None):
    if type(maximum) is not int or not 0 <= maximum <= 1000:
        raise ValueError('invalid watchdog action limit')
    if maximum == 0:
        return False
    home = Path(home)
    parent = home.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o022):
        raise ValueError('watchdog fleet home must be owned and safely permissioned')
    directory = home / '.watchdog'
    directory.mkdir(mode=0o700, exist_ok=True)
    checked(directory.lstat(), directory=True)
    lock_fd = os.open(directory / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(lock_fd, 'r+b') as lock:
        checked(os.fstat(lock.fileno()))
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = directory / 'rate.json'
        now = int(time.time()) if now is None else now
        if type(now) is not int or now < 0:
            raise ValueError('invalid watchdog clock')
        row = {'window_start': now, 'count': 0}
        try:
            fd = os.open(state, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd, 'rb') as source:
                checked(os.fstat(source.fileno()))
                body = source.read(257)
            if len(body) > 256:
                raise ValueError('watchdog state exceeds bound')
            row = json.loads(body)
            if (not isinstance(row, dict) or set(row) != {'window_start', 'count'}
                    or any(type(row[key]) is not int or row[key] < 0 for key in row)
                    or row['count'] > 1000):
                raise ValueError('invalid watchdog rate state')
        if now - row['window_start'] >= 3600:
            row = {'window_start': now, 'count': 0}
        if row['count'] >= maximum:
            return False
        row['count'] += 1
        fd, name = tempfile.mkstemp(prefix='rate-next-', dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w') as target:
                json.dump(row, target, sort_keys=True)
                target.write('\n')
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, state)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return True


if __name__ == '__main__':
    if sys.argv[1:] == ['--guest-admission']:
        try:
            allowed = guest_admission_report(sys.stdin.buffer)
        except (OSError, ValueError, TypeError, OverflowError):
            allowed = False
        if not allowed:
            print('watchdog guest recovery deferred: VM resource budget unavailable', file=sys.stderr)
        raise SystemExit(0 if allowed else 75)
    try:
        if len(sys.argv) != 3:
            raise ValueError('fleet home and action limit required')
        raise SystemExit(0 if admit(sys.argv[1], int(sys.argv[2])) else 1)
    except (OSError, ValueError):
        print('watchdog recovery deferred: private rate state unavailable', file=sys.stderr)
        raise SystemExit(1)
