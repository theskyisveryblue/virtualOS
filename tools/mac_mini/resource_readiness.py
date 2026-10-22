"""Read-only Mini capacity inventory; run on the host using python3 and stdin."""
import datetime
import json
import os
from pathlib import Path
import subprocess
from urllib import request


def report():
    home = Path.home()
    disk = os.statvfs('/System/Volumes/Data')
    result = dict(schema='virtualos-resource-readiness/v1',
                  observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  disk_free_bytes=disk.f_bavail * disk.f_frsize,
                  changes_made=False)
    roots = [home / relative for relative in ('Library/Caches', '.cache',
             'Library/Developer', 'vos-fleet', 'VirtualOSMachines', '.ollama')]
    roots.append(Path('/private/tmp'))
    inventory = []
    for root in roots:
        if not root.exists() or root.is_symlink():
            continue
        try:
            scan = subprocess.run(['du', '-k', '-d', '1', str(root)],
                                  capture_output=True, text=True, timeout=20)
            rows = []
            for line in scan.stdout.splitlines():
                size, _, path = line.partition('\t')
                if size.isdigit():
                    rows.append(dict(path=path, allocated_bytes=int(size) * 1024))
            inventory.append(dict(root=str(root), complete=scan.returncode == 0,
                                  largest=sorted(rows, key=lambda row: row['allocated_bytes'], reverse=True)[:12]))
        except (OSError, subprocess.SubprocessError) as error:
            inventory.append(dict(root=str(root), error=type(error).__name__))
    result['storage'] = inventory
    try:
        scan = subprocess.run(['ps', '-axo', 'pid=,rss=,comm='], check=True,
                              capture_output=True, text=True, timeout=5)
        rows = []
        for line in scan.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append(dict(pid=int(parts[0]), rss_bytes=int(parts[1]) * 1024,
                                 executable=parts[2]))
        result['largest_processes'] = sorted(rows, key=lambda row: row['rss_bytes'], reverse=True)[:20]
    except (OSError, subprocess.SubprocessError) as error:
        result['process_error'] = type(error).__name__
    opener = request.build_opener(request.ProxyHandler({}))
    try:
        with opener.open('http://127.0.0.1:11434/api/ps', timeout=5) as response:
            body = response.read(262145)
        if len(body) > 262144:
            raise ValueError('model metadata exceeds bound')
        row = json.loads(body)
        result['loaded_models'] = [{key: model.get(key) for key in
            ('name', 'size', 'size_vram', 'expires_at')} for model in row['models']]
    except (OSError, ValueError, KeyError, TypeError) as error:
        result['model_metadata_error'] = type(error).__name__
    return result


if __name__ == '__main__':
    print(json.dumps(report(), sort_keys=True))
