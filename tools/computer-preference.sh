#!/usr/bin/env bash
# Store explicit local design preferences without retaining image pixels.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
private_dir="$repo_dir/tools/.private/design"
journal="$private_dir/preferences.jsonl"
models="$repo_dir/tools/models/design"
case "${1:-}" in
  --vote)
    [[ $# -eq 6 ]] || { echo 'Usage: --vote FAMILY DIMENSION LEFT RIGHT left|right' >&2; exit 2; }
    python3 -B "$repo_dir/tools/design_preferences.py" record \
      --journal "$journal" --models-dir "$models" --family "$2" \
      --dimension "$3" --left "$4" --right "$5" --winner "$6"
    exec python3 -B "$repo_dir/tools/design_preferences.py" train \
      --journal "$journal" --models-dir "$models" --dimension "$3" \
      --output "$private_dir/$3.json"
    ;;
  --status)
    [[ $# -eq 1 ]] || exit 2
    exec python3 -B "$repo_dir/tools/design_preferences.py" status \
      --journal "$journal" --models-dir "$models"
    ;;
  --train)
    [[ $# -eq 2 ]] || { echo 'Usage: --train DIMENSION' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/design_preferences.py" train \
      --journal "$journal" --models-dir "$models" --dimension "$2" \
      --output "$private_dir/$2.json"
    ;;
  *)
    echo 'Usage: ./tools/computer-preference.sh --vote FAMILY DIMENSION LEFT RIGHT left|right|--status|--train DIMENSION' >&2
    exit 2
    ;;
esac
