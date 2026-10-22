"""Legacy poller relay transport: protected credentials, stdin bodies, bounded HTTP."""
import argparse
import os
from pathlib import Path
import re
import signal
import sys
import urllib.error
import urllib.request

from fleet_push import load_token_file, _open
from relay_read import RELAY

MAX_BODY = 8 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024


def deadline_expired(signum, frame):
    raise TimeoutError('legacy relay deadline exceeded')


def request(method, path, body, content_type, token_file, relay=RELAY):
    routes = {'GET': r'/cmd/poll',
              'POST': r'/(?:status|usage|cmd/[A-Za-z0-9_-]{1,128}/result)',
              'PUT': r'/(?:screenshot|usage/artifacts/[A-Za-z0-9_.-]{1,200})'}
    if (relay.rstrip('/') != RELAY or method not in routes
            or not re.fullmatch(routes[method], path)
            or content_type not in ('application/json', 'image/jpeg')
            or len(body) > MAX_BODY or method == 'GET' and body):
        raise ValueError('legacy relay request refused')
    token = load_token_file(token_file)
    req = urllib.request.Request(RELAY + path, data=None if method == 'GET' else body,
        method=method, headers={'Authorization': 'Bearer ' + token,
                               'Content-Type': content_type,
                               'User-Agent': 'virtualos-legacy-poller/1.0'})
    try:
        with _open(req, timeout=30 if method == 'GET' else 20) as response:
            result = response.read(MAX_RESPONSE + 1)
            if len(result) > MAX_RESPONSE:
                raise ValueError('legacy relay response exceeds bound')
            return result
    except urllib.error.HTTPError as failure:
        failure.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--relay', default=RELAY)
    parser.add_argument('--method', choices=('GET', 'POST', 'PUT'), required=True)
    parser.add_argument('--path', required=True)
    parser.add_argument('--content-type', default='application/json')
    args = parser.parse_args()
    # Ignore ambient bearer values; only the validated file can authorize this request.
    os.environ.pop('AUTH_TOKEN', None)
    os.environ.pop('VOS_AUTH_TOKEN', None)
    previous = signal.signal(signal.SIGALRM, deadline_expired)
    signal.alarm(31 if args.method == 'GET' else 21)
    try:
        body = b'' if args.method == 'GET' else sys.stdin.buffer.read(MAX_BODY + 1)
        sys.stdout.buffer.write(request(args.method, args.path, body, args.content_type,
                                       Path(args.token_file), args.relay))
    except (OSError, ValueError):
        raise SystemExit('legacy relay request failed')
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    main()
