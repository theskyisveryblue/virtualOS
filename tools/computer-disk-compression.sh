#!/usr/bin/env bash
# Disposable sparse RAW compression experiment; production VM is never read.
set -euo pipefail
[[ $# -eq 1 && "$1" == --measure ]] || {
  echo 'Usage: ./tools/computer-disk-compression.sh --measure' >&2; exit 2;
}
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 -B "$repo_dir/tools/disk_compression_experiment.py"
