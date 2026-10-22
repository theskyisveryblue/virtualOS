"""Private SSH multiplexing and an independent read-only relay health path."""
import json
import os
from pathlib import Path
import re
import stat
from urllib import request


def _read_private(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_nlink != 1):
            raise PermissionError('connection settings require a private owned regular file')
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ValueError('connection settings exceed bound')
    return body


def control_options(directory):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('SSH control directory must be private and owned')
    path = directory / 'ssh-control'
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError('SSH control path must be a private owned socket')
    options = ['-o', 'ControlMaster=auto', '-o', 'ControlPersist=600', '-o', 'ControlPath=' + str(path)]
    route = directory / 'ssh-route.json'
    if route.exists() or route.is_symlink():
        info = route.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 128):
            raise PermissionError('SSH route must be a private owned regular file')
        value = json.loads(_read_private(route, 128))
        if value != {'route': 'pinned-lan'}:
            raise ValueError('unknown SSH route')
        # Keep the protected config's identity and HostKeyAlias. Only bypass
        # the unavailable Cloudflare transport for this known, pinned host.
        options += ['-o', 'ProxyCommand=none', '-o', 'HostName=192.168.1.123']
    return options


def relay_status(token_path=None, *, opener=None):
    token_path = token_path or Path.home() / '.config/virtualos/browser-token'
    for path, directory in ((token_path.parent, True), (token_path, False)):
        info = path.lstat()
        kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (not kind or info.st_uid != os.getuid() or info.st_mode & 0o077
                or not directory and info.st_nlink != 1):
            raise PermissionError('browser credential must remain private and owned')
    token = _read_private(token_path, 4096).decode('utf-8').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.\-]{20,256}', token):
        raise ValueError('invalid browser credential format')
    from cloud_disk_r2 import _NoRedirect
    opener = opener or request.build_opener(request.ProxyHandler({}), _NoRedirect)
    req = request.Request('https://virtualos-relay.ai-c24.workers.dev/fleet/status',
                          headers={'Authorization': 'Bearer ' + token,
                                   'User-Agent': 'virtualos-observer/1.0'})
    with opener.open(req, timeout=10) as response:
        body = response.read(262145)
        if response.status != 200 or len(body) > 262144:
            raise ValueError('invalid relay status response')
    row = json.loads(body)
    if not isinstance(row, dict) or row.get('ok') is not True:
        raise ValueError('relay status unavailable')
    live = row.get('liveness', {})
    fleet = row.get('fleet', {})
    if not isinstance(live, dict) or not isinstance(fleet, dict) or not isinstance(fleet.get('instances', []), list):
        raise ValueError('invalid relay status shape')
    age = live.get('lastHeartbeatAgeSec')
    age = age if type(age) is int and age >= 0 else None
    instances = []
    for vm in fleet.get('instances', [])[:256]:
        if not isinstance(vm, dict):
            continue
        name = vm.get('name')
        if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,64}', name):
            state = vm.get('state')
            instances.append(dict(name=name, ready=vm.get('ready') is True,
                state=state if state in ('running', 'stopped', 'starting', 'stopping', 'queued', 'failed') else 'unknown'))
    return dict(isStale=live.get('isStale') is not False or age is None,
                lastHeartbeatAgeSec=age, instances=instances)
