"""Bounded direct-tunnel verification without credentials in process arguments."""
from contextlib import contextmanager
import json
import http.client
import os
import signal
from pathlib import Path
import tempfile
from urllib import error, parse, request

MAX_FRAME = 8 * 1024**2
MAX_HEALTH = 1024**2


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HTTP = request.build_opener(request.ProxyHandler({}), NoRedirect)


def configuration(environ):
    base = environ.get('REMOTE_URL', '').rstrip('/')
    if not base or any(ord(c) < 33 or ord(c) > 126 for c in base):
        raise ValueError('REMOTE_URL contains unsupported characters')
    url = parse.urlsplit(base)
    if (url.scheme != 'https' or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError('REMOTE_URL must be an HTTPS base URL without credentials, query or fragment')
    url.port  # Reject malformed port syntax before reading credentials.
    headers = {'User-Agent': 'virtualos-private-verifier/1.0'}
    for variable, header in (('CF_ACCESS_CLIENT_ID', 'CF-Access-Client-Id'),
                             ('CF_ACCESS_CLIENT_SECRET', 'CF-Access-Client-Secret'),
                             ('VOS_AUTH_TOKEN', 'Authorization')):
        value = environ.get(variable, '')
        if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError('required private verification credential is missing or invalid')
        headers[header] = 'Bearer ' + value if header == 'Authorization' else value
    return base, headers


def bounded_get(url, headers, maximum, opener=HTTP):
    try:
        with opener.open(request.Request(url, headers=headers), timeout=15) as response:
            if response.status != 200:
                raise ValueError('verification endpoint did not return 200')
            data = response.read(maximum + 1)
    except error.HTTPError as failure:
        failure.close()
        raise ValueError('verification endpoint refused the request') from None
    if len(data) > maximum:
        raise ValueError('verification response exceeds its byte limit')
    return data


def verify(environ, output_root=None, fetch=bounded_get):
    base, headers = configuration(environ)
    health = fetch(base + '/health', headers, MAX_HEALTH)
    state = json.loads(health)
    if not isinstance(state, dict) or state.get('ok') is not True:
        raise ValueError('verification health response is not healthy')
    image = fetch(base + '/frame.jpg?source=window&app=Codex&max=1400', headers, MAX_FRAME)
    if len(image) < 10000 or not image.startswith(b'\xff\xd8') or not image.endswith(b'\xff\xd9'):
        raise ValueError('verification frame is missing, truncated or too small')
    # Only allocate an output after both responses pass. Unique private paths
    # cannot follow the old predictable /tmp/virtualos-remote-verify.jpg link.
    directory = Path(tempfile.mkdtemp(prefix='virtualos-remote-verify-', dir=output_root))
    try:
        descriptor, name = tempfile.mkstemp(prefix='frame-', suffix='.jpg', dir=directory)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(image)
        return {'verified': True, 'verification_scope': 'health_and_jpeg_envelope',
                'health_http': 200, 'frame_http': 200,
                'frame_bytes': len(image), 'frame_file': name}
    except BaseException:
        # This directory and its single output were created by this call.
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
        raise


@contextmanager
def verification_deadline(seconds=35):
    # Socket timeouts reset on each read; a slow stream must not keep this
    # standalone verification process alive indefinitely.
    def expired(*args):
        raise TimeoutError('private verification deadline')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def main():
    # Retain the documented environment interface, then remove the credentials
    # from the Python environment before any subsequent work. No child process
    # receives them and no shell expands them into curl arguments.
    settings = {key: os.environ.pop(key, '') for key in
                ('REMOTE_URL', 'CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET', 'VOS_AUTH_TOKEN')}
    try:
        with verification_deadline():
            print(json.dumps(verify(settings), sort_keys=True))
    except (OSError, ValueError, UnicodeError, http.client.HTTPException, RecursionError):
        raise SystemExit('private remote verification failed; no credential values logged')


if __name__ == '__main__':
    main()
