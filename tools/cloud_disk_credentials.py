"""Load protected R2 credentials without executing an env file or copying keys.

An existing account API token can supply its current S3 identity. Token IDs in
old metadata can become stale after a rotation, so verify against the account.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
from urllib import error, request

ACCOUNT = 'c24a3fb4bedba669785c18c834f51921'
ENDPOINT = 'https://' + ACCOUNT + '.r2.cloudflarestorage.com'


def read_private_env(path):
    path = Path(path)
    for target, directory in ((path.parent, True), (path, False)):
        info = target.lstat()
        kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (not kind or info.st_uid != os.getuid() or info.st_mode & 0o077
                or (not directory and info.st_nlink != 1)):
            raise PermissionError('private owned credential file and directory required')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_nlink != 1):
            raise PermissionError('private owned credential descriptor required')
        body = source.read(65537)
    if len(body) > 65536:
        raise ValueError('credential file exceeds bound')
    values = {}
    for line in body.decode('utf-8').splitlines():
        words = shlex.split(line, comments=True)
        if words[:1] == ['export']:
            words = words[1:]
        if not words:
            continue
        if len(words) != 1 or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0]):
            raise ValueError('credential file requires literal variable assignments')
        name, value = words[0].split('=', 1)
        if name in values:
            raise ValueError('duplicate credential assignment')
        values[name] = value
    return values


def verify_account_token(token):
    from cloud_disk_r2 import _NoRedirect
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect)
    req = request.Request('https://api.cloudflare.com/client/v4/accounts/' + ACCOUNT + '/tokens/verify',
                          headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise ValueError('invalid token verification response')
    except error.HTTPError as failure:
        status = failure.code
        failure.close()
        if status in (408, 429, 500, 502, 503, 504):
            raise ConnectionError('account token verification temporarily unavailable') from None
        raise PermissionError('account token verification refused with HTTP %d' % status) from None
    except error.URLError:
        raise ConnectionError('account token verification unavailable') from None
    row = json.loads(body)
    if not isinstance(row, dict) or row.get('success') is not True:
        raise PermissionError('account token verification rejected')
    result = row.get('result', {})
    if not isinstance(result, dict):
        raise ValueError('invalid account token identity response')
    return result


def credentials(path, *, verify=verify_account_token):
    values = read_private_env(path)
    if values.get('CLOUDFLARE_ACCOUNT_ID') != ACCOUNT or values.get('CLOUDFLARE_R2_ENDPOINT', ENDPOINT) != ENDPOINT:
        raise ValueError('R2 credential account or endpoint differs from pin')
    access, secret = values.get('AWS_ACCESS_KEY_ID'), values.get('AWS_SECRET_ACCESS_KEY')
    if not access or not secret:
        token = values.get('CLOUDFLARE_API_TOKEN', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{30,256}', token):
            raise ValueError('R2 credentials unavailable')
        result = verify(token)
        access = result.get('id')
        if result.get('status') != 'active' or not isinstance(access, str) or not re.fullmatch(r'[0-9a-f]{32}', access):
            raise PermissionError('active account token identity required')
        secret = hashlib.sha256(token.encode()).hexdigest()
    if (not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', access)
            or not re.fullmatch(r'[A-Za-z0-9/+=_-]{16,256}', secret)):
        raise ValueError('invalid R2 credential format')
    env = dict(CLOUDFLARE_ACCOUNT_ID=ACCOUNT, CLOUDFLARE_R2_ENDPOINT=ENDPOINT,
                AWS_ACCESS_KEY_ID=access, AWS_SECRET_ACCESS_KEY=secret, AWS_REGION='auto',
                AWS_DEFAULT_REGION='auto', AWS_EC2_METADATA_DISABLED='true')
    if values.get('AWS_ACCESS_KEY_ID') and values.get('AWS_SESSION_TOKEN'):
        session = values['AWS_SESSION_TOKEN']
        if not re.fullmatch(r'[A-Za-z0-9_/+=.\-]{16,16384}', session):
            raise ValueError('invalid temporary R2 session token')
        env['AWS_SESSION_TOKEN'] = session
    return env


def main():
    import subprocess
    import sys
    import time
    from cloud_disk_archive_delegate import operator, paths, save_status
    os.umask(0o077)
    if len(sys.argv) != 3 or sys.argv[2] not in ('run', 'recover'):
        raise SystemExit('invalid credential operator mode')
    prepared = False
    try:
        env = credentials(sys.argv[1])
        for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                     'CLOUDFLARE_API_TOKEN', 'CF_API_TOKEN'):
            os.environ.pop(name, None)
        os.environ.update(env)
        prepared = True
        if sys.argv[2] == 'run':
            operator()
        else:
            result = operator('recover')
            if result.get('state') in ('waiting_for_capacity', 'waiting_for_resources'):
                raise SystemExit(3)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as failure:
        if not prepared:
            save_status(paths(), dict(state='failed', error=type(failure).__name__,
                        retryable_connection_failure=isinstance(failure, (ConnectionError, TimeoutError)),
                        updated_at=time.time()))
        print(json.dumps({'archive_operator': 'failed', 'error': type(failure).__name__}), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
