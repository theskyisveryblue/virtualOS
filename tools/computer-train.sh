#!/usr/bin/env bash
# Reproduce compact public-data training without changing the served models.
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^(--router|--critique|--design)$ ]]; then
  echo 'Usage: ./tools/computer-train.sh --router|--critique|--design' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
family="${1#--}"
run_dir="$(mktemp -d "/private/tmp/virtualos-${family}-train.XXXXXX")"
chmod 700 "$run_dir"
echo "train=started family=$family output=$run_dir"
if [[ "$family" == design ]]; then
  for kind in alignment overlap whitespace; do
    shard_dir="$run_dir/absolute-human-$kind"
    mkdir -p "$shard_dir"
    shard="$shard_dir/train-00000-of-00001.parquet"
    curl --fail --location --silent --show-error --max-time 180 --max-filesize 50000000 \
      "https://huggingface.co/datasets/creative-graphic-design/GraphicDesignEvaluation/resolve/a503deef9686671615cbd76b653fff5bbc823fb1/absolute-human-$kind/train-00000-of-00001.parquet" \
      --output "$shard"
    PYTHONPATH="$repo_dir/tools" python3 -B "$repo_dir/tools/models/design/train.py" \
      --kind "$kind" --train "$shard" --output "$run_dir/$kind"
  done
  reference_dir="$repo_dir/tools/models/design"
elif [[ "$family" == router ]]; then
  python3 -B "$repo_dir/tools/models/ui_router/train.py" --output "$run_dir"
  reference_dir="$repo_dir/tools/models/ui_router"
else
  source_file="$run_dir/uicrit_public.csv"
  curl --fail --location --silent --show-error --max-time 120 --max-filesize 6000000 \
    'https://raw.githubusercontent.com/google-research-datasets/uicrit/main/uicrit_public.csv' \
    --output "$source_file"
  python3 -B "$repo_dir/tools/models/critique/train.py" \
    --train "$source_file" --output "$run_dir"
  reference_dir="$repo_dir/tools/models/critique"
fi
python3 -B - "$run_dir" "$reference_dir" "$family" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

new_dir, reference_dir = map(Path, sys.argv[1:3])
family = sys.argv[3]
if family == "design":
    kinds = ("alignment", "overlap", "whitespace")
    file_pairs = tuple((new_dir / kind / filename,
                        reference_dir / f"{kind}{'.metrics' if filename == 'metrics.json' else ''}.json")
                       for kind in kinds for filename in ("model.json", "metrics.json"))
    for kind in kinds:
        held_out = json.loads((new_dir / kind / "metrics.json").read_text())["holdout"]
        if held_out["pair_accuracy"] <= held_out["chance_pair_accuracy"]:
            raise SystemExit(f"train=refused: {kind} did not beat held-out chance")
elif family == "router":
    metrics = json.loads((new_dir / "metrics.json").read_text())
    if metrics["test"]["correct"] <= metrics["test"]["majority_baseline"]:
        raise SystemExit("train=refused: router did not beat held-out majority baseline")
else:
    metrics = json.loads((new_dir / "metrics.json").read_text())
    if not metrics["promotable"]:
        raise SystemExit("train=refused: critique model did not beat held-out baseline")
if family != "design":
    file_pairs = tuple((new_dir / name, reference_dir / name)
                       for name in ("model.json", "metrics.json"))
matched = True
for new_path, old_path in file_pairs:
    new = new_path.read_bytes()
    old = old_path.read_bytes()
    matched &= new == old
    print(json.dumps({"file": str(new_path.relative_to(new_dir)), "bytes": len(new),
                      "sha256": hashlib.sha256(new).hexdigest(),
                      "matches_served_source": new == old}, sort_keys=True))
if not matched:
    raise SystemExit(f"train=reproduced_but_artifacts_differ family={family}")
print(f"train=verified family={family}")
PY
