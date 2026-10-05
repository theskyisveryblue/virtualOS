#!/usr/bin/env bash
# Offline ratings from a human-written UI critique file; no image capture.
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -ne 2 || "$1" != --score ]]; then
  echo 'Usage: ./tools/computer-critique.sh --score CRITIQUE.txt' >&2
  exit 2
fi
exec python3 -B "$repo_dir/tools/critique_infer.py" --score "$2" \
  --model "$repo_dir/tools/models/critique/model.json"
