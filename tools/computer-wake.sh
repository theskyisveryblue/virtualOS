#!/usr/bin/env bash
# Explicitly wake a black guest display with one key, then recheck the image.
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo 'Usage: ./tools/computer-wake.sh' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
before="$("$repo_dir/tools/vosctl" computer doctor)"
if printf '%s' "$before" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("screenshot_usable") else 1)'; then
  echo 'guest_display=already_usable'
  exit 0
fi
echo 'guest_display=blank; sending one space key'
"$repo_dir/tools/vosctl" key space
for delay in 1 2 3; do
  sleep "$delay"
  after="$("$repo_dir/tools/vosctl" computer doctor)"
  if printf '%s' "$after" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("screenshot_usable") else 1)'; then
    echo 'guest_display=usable'
    exit 0
  fi
done
echo 'guest_display=still_blank_after_one_key; no further input sent' >&2
exit 1
