#!/usr/bin/env bash
# Offline interface-mode advice from a compact public-data model.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -ne 2 || "$1" != --suggest ]]; then
  echo 'Usage: ./tools/computer-route.sh --suggest "TASK"' >&2
  exit 2
fi
exec python3 -B "$repo_dir/tools/hf_ui_router_train.py" --suggest "$2" \
  --model "$repo_dir/tools/models/ui_router/model.json"
