"""Admission and pressure monitoring for one disposable 1 CPU / 1 GiB guest."""
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time


def policy(report, *, running=False):
    fields = ('disk_free_kib', 'swap_used_mib', 'memory_free_percent', 'load_one', 'cpu_count')
    if any(type(report.get(key)) not in (int, float) or not math.isfinite(report[key]) for key in fields):
        return {'linux_lab': 'paused', 'reasons': ['resource_measurement_unavailable']}
    reasons = []
    if report['disk_free_kib'] < (8 if running else 12) * 1024**2:
        reasons.append('disk_headroom')
    if report['swap_used_mib'] < 0 or report['swap_used_mib'] >= 16 * 1024:
        reasons.append('swap_pressure')
    if not (25 if running else 40) <= report['memory_free_percent'] <= 100:
        reasons.append('memory_headroom')
    if report['cpu_count'] < 2 or not 0 <= report['load_one'] < (2 if running else 1) * report['cpu_count']:
        reasons.append('cpu_headroom')
    return dict(linux_lab='paused' if reasons else 'allowed', reasons=reasons,
                budget={'cpus': 1, 'memory_gib': 1}, **{key: report[key] for key in fields})


def collect():
    script = Path(__file__).with_name('computer-resources.sh')
    result = subprocess.run(['bash', str(script), '--report'], capture_output=True,
                            text=True, timeout=15, check=True)
    return json.loads(result.stdout)


def private_output_parent(path):
    if path.is_symlink():
        raise ValueError('linked Linux output directory refused')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError('Linux output directory must be private and owned by this user')


def watch(pid, flag, *, report=collect, pause=time.sleep, alive=None, deadline=None):
    alive = alive or (lambda: os.kill(pid, 0))
    deadline = deadline if deadline is not None else time.monotonic() + 900
    while True:
        try:
            alive()
        except ProcessLookupError:
            return
        try:
            result = policy(report(), running=True)
        except (OSError, ValueError, subprocess.SubprocessError):
            result = {'linux_lab': 'paused', 'reasons': ['resource_measurement_unavailable']}
        if time.monotonic() >= deadline:
            result = {'linux_lab': 'paused', 'reasons': ['guest_runtime_limit']}
        if result['linux_lab'] != 'allowed':
            # The owning shell stops its own child. This monitor never sends
            # signals to guest processes or unrelated applications.
            with flag.open('x') as output:
                json.dump(result, output, sort_keys=True)
            print(json.dumps(result, sort_keys=True), flush=True)
            return
        pause(5)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        if len(sys.argv) == 3 and sys.argv[1] == 'prepare-output':
            private_output_parent(Path(sys.argv[2]))
            print(json.dumps({'linux_output_directory': 'ready'}))
            raise SystemExit(0)
        if len(sys.argv) == 2 and sys.argv[1] == 'check':
            result = policy(collect())
            print(json.dumps(result, sort_keys=True))
            raise SystemExit(0 if result['linux_lab'] == 'allowed' else 75)
        if len(sys.argv) != 4 or sys.argv[1] != 'watch' or int(sys.argv[2]) <= 1:
            raise ValueError('invalid bounded Linux resource operation')
        flag = Path(sys.argv[3])
        info = flag.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or flag.exists() or flag.is_symlink()):
            raise ValueError('private unused resource flag required')
        watch(int(sys.argv[2]), flag)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({'linux_lab': 'paused', 'error': type(error).__name__}))
        raise SystemExit(2 if sys.argv[1:2] == ['prepare-output'] else 75)
