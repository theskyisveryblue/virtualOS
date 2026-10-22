#!/usr/bin/env bash
# Read-only audit of the three private Hugging Face compact-model repositories.
set -euo pipefail

[[ "${1:-}" == --verify && $# -eq 1 ]] || {
  echo 'Usage: ./tools/computer-hf.sh --verify' >&2
  exit 2
}
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 -B - "$repo_dir" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.errors import RepositoryNotFoundError

root = Path(sys.argv[1])
families = {
    "ui_router": ("fdotinc/virtualos-ui-router-v1", "031f70bd8b7c"),
    "design": ("fdotinc/virtualos-design-principles-v1", "62aab3c22797"),
    "critique": ("fdotinc/virtualos-ui-critique-v1", "0fd57e80792c"),
}
api = HfApi(token=True)
results = []
failed = False
with tempfile.TemporaryDirectory(prefix="vos-hf-verify-") as temp:
    for family, (repo_id, pinned_prefix) in families.items():
        local_dir = root / "tools" / "models" / family
        expected = sorted(path.name for path in local_dir.glob("*.json"))
        try:
            info = api.model_info(repo_id, files_metadata=True)
        except RepositoryNotFoundError:
            failed = True
            results.append({"family": family, "repo": repo_id,
                            "status": "unavailable_to_current_token"})
            continue
        remote_files = {item.rfilename for item in info.siblings}
        missing = sorted(set(expected) - remote_files)
        mismatches = []
        for name in expected:
            if name in missing:
                continue
            remote = Path(hf_hub_download(repo_id, name, revision=info.sha, token=True,
                                          local_dir=Path(temp) / family))
            local_hash = hashlib.sha256((local_dir / name).read_bytes()).hexdigest()
            remote_hash = hashlib.sha256(remote.read_bytes()).hexdigest()
            if local_hash != remote_hash:
                mismatches.append(name)
        pinned = info.sha.startswith(pinned_prefix)
        passed = bool(expected) and pinned and not missing and not mismatches
        failed |= not passed
        results.append({"family": family, "repo": repo_id, "head": info.sha,
                        "pinned_head": pinned, "local_json_files": len(expected),
                        "missing": missing, "mismatches": mismatches,
                        "status": "passed" if passed else "failed"})
print(json.dumps({"hf_compact_models": "failed" if failed else "passed",
                  "repositories": results}, sort_keys=True))
raise SystemExit(1 if failed else 0)
PY
