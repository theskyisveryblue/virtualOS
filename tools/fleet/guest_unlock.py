"""Unlock a visually confirmed fleet login screen using Mini-local credentials."""
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

from brain import FleetVM


def desktop(entries):
    return any(row.get('text', '').strip() in ('Finder', 'Terminal', 'Safari', 'Chrome', 'Notes', 'System Settings')
               and 0 <= row.get('x', -1) <= 0.25 and row.get('y', -1) >= 0.92 for row in entries)


def password_prompt(entries):
    clock = any(re.fullmatch(r'\d{1,2}:\d{2}', row.get('text', '').strip())
                and 0.25 <= row.get('x', -1) <= 0.70
                and 0.65 <= row.get('y', -1) <= 0.92 for row in entries)
    if not clock:
        return None
    return next((row for row in entries if row.get('text', '').strip().lower() == 'enter password'
                 and 0.35 <= row.get('x', -1) <= 0.65
                 and 0.04 <= row.get('y', -1) <= 0.20), None)


def configured_password(path):
    path = Path(path)
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077):
        raise ValueError('guest credential directory must remain private')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ValueError('guest credential file must remain private')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('guest credential descriptor must remain private')
        body = source.read(65537)
    if len(body) > 65536:
        raise ValueError('guest credential file exceeds bound')
    password = None
    for line in body.decode('utf-8').splitlines():
        key, _, value = line.strip().removeprefix('export ').partition('=')
        if key == 'VOS_VM_PASSWORD':
            if password is not None:
                raise ValueError('duplicate guest credential assignment')
            parts = shlex.split(value, comments=True)
            if len(parts) == 1 and re.fullmatch(r'[a-z0-9]{1,64}', parts[0]):
                password = parts[0]
            else:
                raise ValueError('supported configured guest credential unavailable')
    if password is not None:
        return password
    raise ValueError('supported configured guest credential unavailable')


def unlock(vm, password_path, guard, *, pause=time.sleep):
    admitted = subprocess.run(['bash', str(guard), '--guard-offline'],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    if admitted.returncode:
        return {'guest_unlock': 'paused', 'reason': 'resource_guard', 'password_submitted': False}
    vm.health()
    frame = vm.frame()
    if frame.likely_blank:
        if vm.key('space') != 200:
            return {'guest_unlock': 'refused', 'reason': 'wake_failed', 'password_submitted': False}
        pause(2)
        frame = vm.frame()
    if not frame.jpeg or frame.likely_blank:
        return {'guest_unlock': 'refused', 'reason': 'blank_frame', 'password_submitted': False}
    entries = vm.ocr(frame)
    if not desktop(entries) and password_prompt(entries) is None:
        entries = vm.ocr(frame, accurate=True)
    if desktop(entries):
        return {'guest_unlock': 'already_unlocked', 'password_submitted': False}
    prompt = password_prompt(entries)
    if prompt is None:
        return {'guest_unlock': 'refused', 'reason': 'login_screen_unconfirmed', 'password_submitted': False}
    current = vm.frame()
    current_prompt = None if current.likely_blank else password_prompt(vm.ocr(current, accurate=True))
    if current_prompt is None:
        return {'guest_unlock':'refused', 'reason':'login_frame_changed', 'password_submitted':False}
    # Lock-screen wallpapers animate. Reconfirm the semantic target on the new
    # frame and use its current geometry instead of requiring identical pixels.
    frame, prompt = current, current_prompt
    x = (prompt['x'] + prompt.get('width', 0) / 2) * (frame.width - 1)
    y = (1 - prompt['y'] - prompt.get('height', 0) / 2) * (frame.height - 1)
    if vm.click(x, y, frame=frame) != 200:
        raise RuntimeError('guest password field could not be focused')
    pause(0.3)
    focused = vm.frame()
    if focused.likely_blank or password_prompt(vm.ocr(focused, accurate=True)) is None:
        return {'guest_unlock':'refused', 'reason':'focused_login_unconfirmed', 'password_submitted':False}
    password = configured_password(password_path)
    for char in password:
        if vm.key(char) != 200:
            raise RuntimeError('guest key delivery failed')
        pause(0.08)
    password = None
    if vm.key('return') != 200:
        raise RuntimeError('guest login submission failed')
    for _ in range(10):
        pause(1.5)
        after = vm.frame()
        if not after.likely_blank and desktop(vm.ocr(after, accurate=True)):
            return {'guest_unlock': 'verified_desktop', 'password_submitted': True,
                    'credential_route': 'loopback_only', 'password_logged': False}
    return {'guest_unlock': 'unverified', 'password_submitted': True, 'automatic_password_retry': False}


if __name__ == '__main__':
    os.umask(0o077)
    tools = Path(__file__).resolve().parent.parent
    try:
        result = unlock(FleetVM(sys.argv[1] if len(sys.argv) == 2 else 'vm-a'),
                        Path.home() / '.config/virtualos/start.env', tools / 'computer-resources.sh')
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(0 if result['guest_unlock'] in ('verified_desktop', 'already_unlocked') else 1)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as failure:
        print(json.dumps({'guest_unlock': 'refused', 'error': type(failure).__name__}))
        raise SystemExit(1)
