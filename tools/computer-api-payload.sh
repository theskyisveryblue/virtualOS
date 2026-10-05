#!/usr/bin/env bash
# Reproducible, synthetic-only model API payload size and CPU experiment.
set -euo pipefail
[[ $# -eq 1 && "$1" == --measure ]] || {
  echo 'Usage: ./tools/computer-api-payload.sh --measure' >&2; exit 2;
}
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 -B "$repo_dir/tools/api_payload_experiment.py"
