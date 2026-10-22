"""Read fleet health without shell-evaluating settings or exposing token argv."""
import argparse
import json
import os
from pathlib import Path

from fleet_push import load_token_file, _get_json

RELAY = 'https://virtualos-relay.ai-c24.workers.dev'


def read_status(home=None, environ=None, token_path=None):
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    if environ.get('RELAY_URL', RELAY).rstrip('/') != RELAY:
        raise ValueError('fleet health requires the pinned project relay')
    browser = home / '.config/virtualos/browser-token'
    host = home / '.config/virtualos/relay.env'
    # Prefer the narrower browser credential. An invalid explicit credential
    # fails closed instead of silently falling back to the host credential.
    if token_path is not None:
        token = load_token_file(token_path)
    elif browser.exists() or browser.is_symlink():
        token = load_token_file(browser)
    elif environ.get('AUTH_TOKEN') or environ.get('VOS_AUTH_TOKEN'):
        token = environ.get('AUTH_TOKEN') or environ['VOS_AUTH_TOKEN']
    else:
        token = load_token_file(host)
    row = _get_json(RELAY + '/fleet/status', token, timeout=6)
    if not isinstance(row, dict) or row.get('ok') is not True:
        raise ValueError('relay health unavailable')
    return row


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file')
    args = parser.parse_args()
    try:
        print(json.dumps(read_status(token_path=args.token_file), sort_keys=True))
    except (OSError, ValueError):
        # Never include request objects, URLs, settings or token values in errors.
        raise SystemExit(1)
