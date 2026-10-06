#!/usr/bin/env bash
# Bounded public macOS UI dataset audit; exports counts only.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -ne 2 || "$1" != --uipad-audit ]]; then
  echo 'Usage: ./tools/computer-data.sh --uipad-audit DATASET_DIRECTORY' >&2
  exit 2
fi
exec python3 -B "$repo_dir/tools/macos_ax_audit.py" "$2" \
  --write "$repo_dir/tools/data/uipad-audit.json"
