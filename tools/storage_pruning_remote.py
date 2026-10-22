"""One bounded, resource-gated snapshot audit; executed over pinned SSH."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time


def run(sources):
    root = Path('/Users/ai/vos-fleet')
    source = root / '.virtualos-nbd/vm-a/overlay.sqlite'
    config = root / 'shared-disks/vm-a.json'
    original = root / 'vm-a-original-20261021.bundle/Disk.img'
    config_bytes = config.read_bytes()
    cfg = json.loads(config_bytes)
    if (cfg.get('baseName') != 'vm-a-base-20261021'
            or cfg.get('delta') != str(root / 'deltas/vm-a-compressed-20261021.sqlite')
            or cfg.get('deltaFormat') != 'compressed-v1' or cfg.get('writeFormat') is not None
            or cfg.get('deltaSHA256') != 'ed2a522a2ff69e3210927c83df8784478794474d849371215168f8bd8332ba6b'):
        raise ValueError('unexpected live backing configuration')
    base = root / (cfg['baseName'] + '.bundle') / 'Disk.img'
    seed = Path(cfg['delta'])
    resources = {}
    started = time.monotonic()
    last_report = -float('inf')

    def guard():
        nonlocal resources, last_report
        now = time.monotonic()
        if now - started > 600:
            raise TimeoutError('audit time budget exceeded')
        volume = os.statvfs(root)
        if volume.f_bavail * volume.f_frsize < 10 * 1024**3 + 64 * 1024**2:
            raise BlockingIOError('host reserve')
        if os.getloadavg()[0] >= (os.cpu_count() or 1):
            raise BlockingIOError('host load')
        if now - last_report >= 15:
            resources = json.loads(subprocess.check_output(
                ['bash', str(root / 'tools/computer-resources.sh'), '--report'],
                text=True, timeout=20))
            last_report = now
        if (resources.get('memory_free_percent') is None
                or resources['memory_free_percent'] < 50
                or resources.get('swap_used_mib') is None
                or resources['swap_used_mib'] >= 8 * 1024):
            raise BlockingIOError('host memory or swap')
        if config.read_bytes() != config_bytes:
            raise ValueError('backing configuration changed')

    try:
        guard()
        info = source.lstat()
        volume = os.statvfs(root)
        if (source.is_symlink() or not source.is_file() or info.st_uid != os.getuid()
                or not 0 < info.st_size <= 4 * 1024**3):
            raise ValueError('unexpected live overlay')
        if volume.f_bavail * volume.f_frsize < 10 * 1024**3 + 2 * info.st_size + 64 * 1024**2:
            raise BlockingIOError('snapshot headroom')
        before_original = original.stat()
        before_pids = subprocess.check_output(
            ['ps', '-p', '440,2499,80901', '-o', 'pid=,comm='], text=True, timeout=5)
        for pid in (440, 2499, 80901):
            if not any(line.split()[0] == str(pid) for line in before_pids.splitlines()):
                raise ValueError('protected guest process missing')
        private = root / '.local/maintenance/compressed-writes-20261021'
        p = private.lstat()
        if private.is_symlink() or p.st_uid != os.getuid() or p.st_mode & 0o077:
            raise ValueError('private maintenance directory required')
        with tempfile.TemporaryDirectory(prefix='.pruning-audit-', dir=private) as name:
            directory = Path(name)
            for filename, body in sources.items():
                if Path(filename).name != filename or not filename.endswith('.py'):
                    raise ValueError('unexpected source filename')
                (directory / filename).write_text(body)
            sys.dont_write_bytecode = True
            sys.path.insert(0, str(directory))
            from overlay_pruning_audit import audit
            snapshot = directory / 'snapshot.sqlite'
            reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=.25)
            writer = sqlite3.connect(snapshot)
            try:
                reader.execute('PRAGMA query_only=ON')
                if set(reader.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")) != {
                        ('table', 'blocks'), ('table', 'metadata')}:
                    raise ValueError('raw source schema required')
                def progress(status, remaining, total):
                    guard()
                    time.sleep(.02)
                reader.backup(writer, pages=64, progress=progress, sleep=.1)
            finally:
                writer.close()
                reader.close()
            result = audit(base, seed, cfg['deltaSHA256'], snapshot, guard=guard)
            guard()
            after_original = original.stat()
            if ((before_original.st_dev, before_original.st_ino, before_original.st_size,
                 before_original.st_mtime_ns) !=
                    (after_original.st_dev, after_original.st_ino, after_original.st_size,
                     after_original.st_mtime_ns)):
                raise ValueError('preserved original identity changed')
            after_pids = subprocess.check_output(
                ['ps', '-p', '440,2499,80901', '-o', 'pid=,comm='], text=True, timeout=5)
            if after_pids != before_pids:
                raise ValueError('protected guest processes changed')
        return dict(state='verified', observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    audit=result, temporary_snapshot_released=True, original_identity_unchanged=True,
                    protected_processes_unchanged=True, guest_services_changed=False,
                    source_sha256={k: hashlib.sha256(v.encode()).hexdigest() for k, v in sources.items()})
    except BlockingIOError as error:
        return dict(state='waiting_for_resources', reason=str(error), resources=resources,
                    observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    guest_services_changed=False)
