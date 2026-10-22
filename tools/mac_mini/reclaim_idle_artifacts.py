"""Reclaim only reviewed, inactive disposable browser profiles and updater cache."""
import datetime
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

PROFILES = ('64iKcF', 'Br2BJM', 'EFCcAE', 'HwwyUp', 'LdB29p',
            'W4pdkH', 'ikEcGI', 'jgT2sv', 'xiR6Sv')


def reclaim(path, apply=False):
    row = dict(path=str(path), removed=False)
    try:
        before = path.lstat()
        if (not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid()
                or time.time() - before.st_mtime < 24 * 3600):
            raise ValueError('not_an_owned_inactive_directory')
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError('safe_tree_removal_unavailable')
        commands = subprocess.run(['ps', '-axo', 'command='], check=True,
                                  capture_output=True, text=True, timeout=5).stdout
        if str(path) in commands or str(path.resolve()) in commands:
            raise ValueError('process_references_target')
        handles = subprocess.run(['lsof', '-nP', '-t', '+D', str(path)],
                                 capture_output=True, text=True, timeout=10)
        if handles.returncode != 1 or handles.stdout or handles.stderr:
            raise ValueError('open_handles_or_probe_uncertainty')
        size = subprocess.run(['du', '-sk', str(path)], check=True,
                              capture_output=True, text=True, timeout=10)
        allocated = int(size.stdout.split()[0]) * 1024
        if not 0 <= allocated <= 1024**3:
            raise ValueError('target_exceeds_reviewed_size')
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_mtime_ns):
            raise ValueError('target_changed')
        row.update(eligible=True, allocated_bytes=allocated)
        if apply:
            shutil.rmtree(path)
            row['removed'] = True
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        row.update(eligible=False, reason=str(error) if type(error) is ValueError else type(error).__name__)
    return row


def main():
    if sys.argv[1:] not in ([], ['--apply']):
        raise SystemExit('expected no arguments or --apply')
    if Path.home() != Path('/Users/ai'):
        raise SystemExit('Mini ai account required')
    targets = [Path('/private/tmp') / ('xcanvas-browser.' + name) for name in PROFILES]
    targets.append(Path.home() / 'Library/Caches/pen-updater')
    before = shutil.disk_usage('/System/Volumes/Data').free
    rows = [reclaim(path, apply=bool(sys.argv[1:])) for path in targets]
    after = shutil.disk_usage('/System/Volumes/Data').free
    print(json.dumps(dict(schema='virtualos-idle-artifact-reclamation/v1',
        observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        apply=bool(sys.argv[1:]), targets=rows, disk_free_before_bytes=before,
        disk_free_after_bytes=after, measured_free_bytes_gained=after-before), sort_keys=True))


if __name__ == '__main__':
    main()
