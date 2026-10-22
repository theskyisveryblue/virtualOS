#!/usr/bin/env bash
# Verify both plain-JSON and compact-bundle model API paths on loopback.
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
[[ $# -eq 0 ]] || { echo 'Usage: ./tools/computer-model-test.sh' >&2; exit 2; }
exec python3 -B "$repo_dir/tools/test_model_api.py"
