#!/usr/bin/env bash
# Prepare the guest and launch the bounded .sh worker on the Mac mini.
set -euo pipefail

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --quality ) ]]; then
  echo 'Usage: ./tools/computer-start.sh [--quality]' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
"$repo_dir/tools/computer-resources.sh" --guard || {
  echo 'start=refused: host resource pressure' >&2
  exit 1
}
if [[ "${1:-}" == --quality ]]; then
  if pgrep -fl '[c]omputer-night.sh --quality' >/dev/null 2>&1; then
    echo 'start=refused: quality runner already active' >&2
    exit 1
  fi
  if [[ -e "$HOME/.config/virtualos/computer-quality-stop" ]]; then
    echo 'start=refused: quality stop marker exists' >&2
    exit 1
  fi
  log="$HOME/Library/Logs/virtualos-computer-quality.log"
  nohup "$repo_dir/tools/computer-night.sh" --quality >> "$log" 2>&1 < /dev/null &
  pid=$!
  sleep 2
  if ! kill -0 "$pid" 2>/dev/null; then
    echo 'start=failed: quality runner exited' >&2
    exit 1
  fi
  printf 'start=quality-running pid=%s log=%s\n' "$pid" "$log"
  exit 0
fi

floor_kb="${VOS_CUA_MIN_FREE_KB:-3145728}"
free_kb="$(df -Pk /System/Volumes/Data | awk 'NR == 2 {print $4}')"
if [[ ! "$floor_kb" =~ ^[1-9][0-9]*$ || ! "$free_kb" =~ ^[0-9]+$ ]] || (( free_kb < floor_kb )); then
  echo 'start=refused: insufficient disk space' >&2
  exit 1
fi
if pgrep -fl '[v]osctl computer-night' >/dev/null 2>&1; then
  echo 'start=refused: runner already active' >&2
  exit 1
fi
"$repo_dir/tools/computer-wake.sh"
"$repo_dir/tools/computer-unlock.sh"
"$repo_dir/tools/computer-check.sh"
log="$HOME/Library/Logs/virtualos-computer-night.log"
nohup "$repo_dir/tools/computer-night.sh" >> "$log" 2>&1 < /dev/null &
pid=$!
sleep 2
if ! kill -0 "$pid" 2>/dev/null; then
  echo 'start=failed: runner exited; inspect its log' >&2
  exit 1
fi
printf 'start=running pid=%s log=%s\n' "$pid" "$log"
