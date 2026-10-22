#!/usr/bin/env bash
# One shell entry point for read-only Mini status and bounded local proofs.
set -euo pipefail

if [[ $# -ne 1 || ( "$1" != --status && "$1" != --verify ) ]]; then
  echo 'Usage: ./tools/computer-optimize.sh --status|--verify' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
# The Mini's Documents checkout may be cloud-backed and block reads. The
# staged fleet tools are local to the Mini and are the live runtime copy.
mini_tools=/Users/ai/vos-fleet/tools

mini_readonly() {
  ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai "$1"
}

if [[ "$1" == --status ]]; then
  mini_readonly "$mini_tools/computer-resources.sh --report"
  mini_readonly "$mini_tools/computer-resources.sh --stale-simulator-check" \
    || echo 'stale_simulator=none_or_not_eligible'
  mini_readonly "$mini_tools/computer-status.sh"
  mini_readonly "$mini_tools/computer-r2.sh vm-clone-status"
  mini_readonly "$mini_tools/computer-stream-reload.sh --status"
  mini_readonly "$mini_tools/computer-decision.sh --stats"
  mini_readonly "$mini_tools/computer-decision.sh --model-status"
  mini_readonly "$mini_tools/computer-model-api.sh --status-mini"
  exit 0
fi

free_kib="$(df -Pk /private/tmp | awk 'NR==2 {print $4}')"
if [[ ! "$free_kib" =~ ^[0-9]+$ ]] || (( free_kib < 524288 )); then
  echo "optimize_verify=refused: operator disk below 512 MiB free" >&2
  exit 1
fi

"$repo_dir/tools/computer-space.sh" --cloud-disk-self-test
"$repo_dir/tools/computer-dataset.sh" --playbook-self-test
"$repo_dir/tools/computer-check.sh" --retention-self-test
"$repo_dir/tools/computer-decision.sh" --self-test
mini_readonly "$mini_tools/computer-resources.sh --report"
mini_readonly "$mini_tools/computer-r2.sh vm-clone-status"
mini_readonly "$mini_tools/computer-decision.sh --stats"
mini_readonly "$mini_tools/computer-decision.sh --model-status"
mini_readonly "$mini_tools/computer-model-api.sh --self-test-mini"
echo 'optimize_verify=passed local_proofs=4 mini_readonly_checks=5'
