#!/usr/bin/env bash
# Screenshot quality facts only; retains no guest pixels.
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -ne 1 || "$1" != --audit ]]; then
  echo 'Usage: ./tools/computer-frame.sh --audit' >&2
  exit 2
fi
exec python3 -B "$repo_dir/tools/guest_frame_audit.py"
