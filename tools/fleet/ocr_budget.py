"""Admission for one bounded CPU text model; never loads or downloads models."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
import time

GIB = 1024**3


class OCRAdmissionError(ValueError):
    pass


def resources():
    vm = subprocess.check_output(['vm_stat'], text=True, timeout=5)
    page_size = re.search(r'page size of (\d+) bytes', vm)
    fields = {}
    for key in ('Pages free', 'Pages inactive', 'Pages speculative', 'Swapouts'):
        match = re.search(r'^' + key + r':\s*(\d+)\.', vm, re.M)
        if match is None:
            raise OCRAdmissionError('memory statistics unavailable')
        fields[key] = int(match[1])
    if page_size is None or int(page_size[1]) not in (4096, 16384):
        raise OCRAdmissionError('memory page size unavailable')
    swap = subprocess.check_output(['sysctl', 'vm.swapusage'], text=True, timeout=5)
    used = re.search(r'used = ([0-9.]+)M', swap)
    if used is None:
        raise OCRAdmissionError('swap statistics unavailable')
    disk = os.statvfs('/System/Volumes/Data')
    return dict(available_bytes=sum(fields[key] for key in
        ('Pages free', 'Pages inactive', 'Pages speculative')) * int(page_size[1]),
        swapouts=fields['Swapouts'], swap_used_bytes=float(used[1]) * 1024**2,
        disk_free_bytes=disk.f_bavail * disk.f_frsize, cpus=os.cpu_count() or 0,
        load=os.getloadavg()[0])


def check_resources(before, after, model_bytes):
    required = max(6 * GIB, model_bytes * 2 + GIB // 2 + 4 * GIB)
    for row in (before, after):
        for key in ('available_bytes', 'swapouts', 'disk_free_bytes', 'cpus'):
            if type(row.get(key)) is not int or row[key] < 0:
                raise OCRAdmissionError('incomplete resource statistics')
        if (row['available_bytes'] < required or row['disk_free_bytes'] < 8 * GIB
                or row['cpus'] < 2 or type(row.get('load')) not in (int, float)
                or not 0 <= row['load'] < row['cpus']
                or type(row.get('swap_used_bytes')) not in (int, float)
                or not 0 <= row['swap_used_bytes'] < 8 * GIB):
            raise OCRAdmissionError('insufficient CPU model headroom')
    if after['swapouts'] != before['swapouts']:
        raise OCRAdmissionError('active swap-outs defer OCR inference')
    return required


@contextmanager
def admit(model, endpoint, workers, vm_count, fetch, *, probe=resources, pause=time.sleep,
          directory=None):
    if type(workers) is not int or type(vm_count) is not int or workers != 1 or vm_count != 1:
        raise OCRAdmissionError('OCR mode requires one worker and one VM')
    directory = Path(directory) if directory is not None else Path('/private/tmp') / ('virtualos-ocr-' + str(os.getuid()))
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OCRAdmissionError('OCR lock directory must remain private and owned')
        fd = os.open(directory / 'inference.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as failure:
        raise OCRAdmissionError('OCR inference lock unavailable') from failure
    with os.fdopen(fd, 'r+b') as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise OCRAdmissionError('unsafe OCR inference lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as failure:
            raise OCRAdmissionError('another OCR inference session is active') from failure
        try:
            tags = fetch(endpoint.rstrip('/') + '/api/tags')
            models = tags.get('models') if isinstance(tags, dict) else None
            requested = model if ':' in model else model + ':latest'
            matches = [row for row in models if isinstance(row, dict)
                       and row.get('name') == requested] if isinstance(models, list) else []
            if len(matches) != 1:
                raise OCRAdmissionError('installed OCR model identity unavailable')
            chosen = matches[0]
            # A loopback Ollama endpoint can still represent a cloud model.
            # Reject remote routing before any guest text is sent to chat.
            details = chosen.get('details')
            if (chosen.get('remote_host', '') != '' or chosen.get('remote_model', '') != ''
                    or not isinstance(details, dict) or details.get('format') != 'gguf'):
                raise OCRAdmissionError('OCR requires local GGUF weights without remote routing')
            size = chosen.get('size')
            if type(size) is not int or not 0 < size <= GIB // 2:
                raise OCRAdmissionError('OCR model exceeds the 512 MiB weight limit')
            if not isinstance(chosen.get('digest'), str) or not re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', chosen['digest']):
                raise OCRAdmissionError('OCR model digest unavailable')
            active = fetch(endpoint.rstrip('/') + '/api/ps')
            if not isinstance(active, dict) or active.get('models') != []:
                raise OCRAdmissionError('other loaded models defer OCR inference')
            before = probe()
            pause(1)
            after = probe()
            required = check_resources(before, after, size)
            active = fetch(endpoint.rstrip('/') + '/api/ps')
            if not isinstance(active, dict) or active.get('models') != []:
                raise OCRAdmissionError('a model became active during admission')
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as failure:
            if isinstance(failure, OCRAdmissionError):
                raise
            raise OCRAdmissionError('OCR model admission could not be verified') from failure
        # Keep the lock across the complete run. Never catch exceptions from the
        # body as admission failures: inference may already have been requested.
        # Full local manifest-blob reference, not the mutable source tag.
        # Unsupported daemons must refuse it; never fall back to the tag.
        # Ollama reference resolver: github.com/ollama/ollama/blob/main/manifest/paths.go
        digest = chosen['digest'].removeprefix('sha256:')
        def recheck():
            # The lock excludes our cooperating runners, not unrelated clients.
            # keep_alive=0 unloads our previous request; never load again while
            # another model is resident or host pressure has changed.
            try:
                for sample in range(2):
                    active = fetch(endpoint.rstrip('/') + '/api/ps')
                    if not isinstance(active, dict) or active.get('models') != []:
                        raise OCRAdmissionError('loaded models defer the next OCR request')
                    if sample == 0:
                        before = probe()
                        pause(1)
                    else:
                        after = probe()
                check_resources(before, after, size)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as failure:
                if isinstance(failure, OCRAdmissionError):
                    raise
                raise OCRAdmissionError('OCR request headroom could not be verified') from failure
        yield dict(model_bytes=size, minimum_available_bytes=required, workers=1,
                   model_reference='sha256-' + digest, model_sha256=digest,
                   recheck=recheck)
