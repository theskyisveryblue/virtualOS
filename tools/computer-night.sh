#!/usr/bin/env bash
# Run bounded guest-only Dock practice from a .sh entry point.

set -euo pipefail

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --quality ) ]]; then
  echo "Usage: ./tools/computer-night.sh [--quality]" >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${1:-}" == --quality ]]; then
  hours="${VOS_CUA_OVERNIGHT_HOURS:-10}"
  interval="${VOS_CUA_QUALITY_INTERVAL:-300}"
  [[ "$hours" =~ ^[1-9][0-9]*$ && "$interval" =~ ^[1-9][0-9]*$ ]] || {
    echo 'quality=refused: invalid hours or interval' >&2
    exit 2
  }
  end_at="$(( $(date +%s) + hours * 3600 ))"
  stop="$HOME/.config/virtualos/computer-quality-stop"
  last_check=0
  last_free_kb=0
  benchmark_done=0
  benchmark_failures=0
  echo "quality=started hours=$hours interval_seconds=$interval mode=observation_only"
  while (( $(date +%s) < end_at )); do
    if [[ -e "$stop" ]]; then
      echo 'quality=stopped: stop marker present'
      exit 0
    fi
    now="$(date +%s)"
    if ! "$repo_dir/tools/computer-resources.sh" --guard; then
      echo 'quality=paused: host resource pressure'
      sleep "$interval"
      continue
    fi
    free_kb="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
    if (( now - last_check >= 3600 )); then
      if "$repo_dir/tools/computer-grounding.sh" --check-guard; then
        echo 'quality=guard_check_passed'
      else
        echo 'quality=guard_check_failed'
      fi
      "$repo_dir/tools/computer-eval.sh" 60 || echo 'quality=eval_failed'
      for source in "$HOME/Library/Application Support/virtualOS/computer-use" \
                    "$HOME/Library/Application Support/virtualOS/computer-use/journeys"; do
        VOS_CUA_TRACE_DIR="$source" "$repo_dir/tools/computer-dataset.sh" --readiness \
          || echo "quality=readiness_failed source=$source"
      done
      if [[ "${VOS_CUA_QUALITY_CURRICULUM:-0}" == 1 ]]; then
        target="$("$repo_dir/tools/computer-decision.sh" --next-visible-finder-target \
          | python3 -c 'import json,sys; print(json.load(sys.stdin).get("target") or "")')" || target=''
        if [[ "$target" =~ ^(Applications|Downloads|Documents|Desktop|Recents)$ ]]; then
          if "$repo_dir/tools/computer-journey.sh" --one-shot --target "$target"; then
            echo "quality=curriculum_verified target=$target"
          else
            echo "quality=curriculum_deferred target=$target"
          fi
        else
          echo 'quality=curriculum_complete_or_unavailable'
        fi
      fi
      last_check="$now"
    fi
    if [[ ! "$free_kb" =~ ^[0-9]+$ ]]; then
      echo 'quality=disk_check_failed'
    elif (( free_kb < 3145728 )); then
      echo "quality=observation_only low_disk_free_kib=$free_kb"
    else
      echo "quality=observation_only free_kib=$free_kb"
      if (( free_kb >= 6291456 && last_free_kb >= 6291456
            && benchmark_done == 0 && benchmark_failures < 3 )); then
        if "$repo_dir/tools/computer-grounding.sh" --benchmark; then
          echo 'quality=benchmark_complete'
          benchmark_done=1
        else
          benchmark_failures=$((benchmark_failures + 1))
          echo "quality=benchmark_failed count=$benchmark_failures"
        fi
      fi
    fi
    if [[ "$free_kb" =~ ^[0-9]+$ ]]; then
      last_free_kb="$free_kb"
    else
      last_free_kb=0
    fi
    sleep "$interval"
  done
  echo 'quality=finished: ten-hour window elapsed'
  exit 0
fi

export VOS_CUA_OVERNIGHT_ACT="${VOS_CUA_OVERNIGHT_ACT:-1}"
export VOS_CUA_CLOUD_SYNC=0
unset VOS_CUA_ONE_SHOT
"$repo_dir/tools/computer-check.sh"
if pgrep -fl '[v]osctl computer-night' >/dev/null 2>&1; then
  echo 'Computer-use runner is already active' >&2
  exit 1
fi
exec "$repo_dir/tools/vosctl" computer-night
