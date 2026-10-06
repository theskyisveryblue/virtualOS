#!/usr/bin/env bash
# Bounded, read-only Xcode project tasks and local inventory collection.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
case "${1:-}" in
  --inventory)
    [[ $# -eq 1 ]] || exit 2
    exec python3 -B "$repo_dir/tools/xcode_nav.py" inventory --repo "$repo_dir"
    ;;
  --collect)
    [[ $# -eq 1 ]] || exit 2
    exec python3 -B "$repo_dir/tools/xcode_nav.py" collect --repo "$repo_dir"
    ;;
  --task)
    [[ $# -eq 2 ]] || exit 2
    exec python3 -B "$repo_dir/tools/xcode_nav.py" task "$2" --repo "$repo_dir"
    ;;
  --route)
    [[ $# -eq 2 ]] || exit 2
    exec python3 -B "$repo_dir/tools/xcode_nav.py" route "$2" --repo "$repo_dir"
    ;;
  *)
    echo 'Usage: ./tools/computer-xcode.sh --inventory|--collect|--task TASK_ID|--route "REQUEST"' >&2
    exit 2
    ;;
esac
