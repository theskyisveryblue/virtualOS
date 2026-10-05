#!/usr/bin/env bash
# Reproducible weight-precision and weight+codec size experiment. Read-only.
set -euo pipefail
[[ $# -eq 1 && "$1" == --measure ]] || {
  echo 'Usage: ./tools/computer-weight-quant.sh --measure' >&2; exit 2;
}
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
python3 -B "$repo_dir/tools/weight_quant_experiment.py"
python3 -B "$repo_dir/tools/quant_codec_experiment.py"
