#!/usr/bin/env bash
# One bounded guest task. Default is shadow; --act executes with tooltip guard.
set -euo pipefail

mode=()
if [[ "${1:-}" == '--act' ]]; then
  mode=(--act)
  shift
fi
if [[ $# -ne 1 || -z "$1" ]]; then
  echo 'Usage: ./tools/computer-verify.sh [--act] "Dock task"' >&2
  exit 2
fi
if [[ ${#mode[@]} -gt 0 ]]; then
  case "$1" in
    'Click the blue smiling Finder icon in the guest Dock'|\
    'Click the black Terminal icon in the guest Dock'|\
    'Click the blue Safari compass icon in the guest Dock'|\
    'Click the multicolor Google Chrome icon in the guest Dock') ;;
    *) echo 'Act mode accepts only the four verified Dock tasks' >&2; exit 2 ;;
  esac
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
export VOS_CUA_MIN_FREE_KB="${VOS_CUA_MIN_FREE_KB:-1048576}"
"$repo_dir/tools/computer-check.sh"
"$repo_dir/tools/vosctl" computer ollama "$1" --max-steps 1 "${mode[@]}"
