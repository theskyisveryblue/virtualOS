#!/usr/bin/env bash
# Offline advice from tiny human-rated design principle models.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -ne 3 || "$1" != --compare ]]; then
  echo 'Usage: ./tools/computer-design.sh --compare LEFT_IMAGE RIGHT_IMAGE' >&2
  exit 2
fi
exec python3 -B "$repo_dir/tools/design_infer.py" --compare "$2" "$3" \
  --personal-dir "$repo_dir/tools/.private/design"
