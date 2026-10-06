#!/usr/bin/env bash
# Bounded local choice-model checks; no action execution or cloud upload.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
policy_db="${VOS_OFFLINE_POLICY_DB:-$HOME/Library/Application Support/virtualOS/offline-policy/policy.sqlite3}"
case "${1:-}" in
  --self-test)
    [[ $# -eq 1 ]] || { echo 'usage: computer-decision.sh --self-test|--train FILE MODEL|--decide MODEL REQUEST' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/small_decider.py" self-test
    ;;
  --clef-fixture)
    [[ $# -eq 1 ]] || { echo 'usage: computer-decision.sh --clef-fixture' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/small_decider.py" clef-fixture
    ;;
  --train)
    [[ $# -eq 3 ]] || { echo 'usage: computer-decision.sh --train FILE MODEL' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/small_decider.py" train "$2" "$3"
    ;;
  --decide)
    [[ $# -eq 3 ]] || { echo 'usage: computer-decision.sh --decide MODEL REQUEST' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/small_decider.py" decide "$2" "$3"
    ;;
  --observe)
    [[ $# -eq 2 ]] || { echo 'usage: computer-decision.sh --observe FEEDBACK.json' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_policy.py" observe "$2" --db "$policy_db"
    ;;
  --suggest)
    [[ $# -eq 2 ]] || { echo 'usage: computer-decision.sh --suggest REQUEST.json' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_policy.py" suggest "$2" --db "$policy_db"
    ;;
  --stats)
    [[ $# -eq 1 ]] || { echo 'usage: computer-decision.sh --stats' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_policy.py" stats --db "$policy_db"
    ;;
  --next-finder-target)
    [[ $# -eq 1 ]] || { echo 'usage: computer-decision.sh --next-finder-target' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_policy.py" next-finder-target --db "$policy_db"
    ;;
  --next-visible-finder-target)
    [[ $# -eq 1 ]] || { echo 'usage: computer-decision.sh --next-visible-finder-target' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_policy.py" next-visible-finder-target --db "$policy_db"
    ;;
  --ingest-traces)
    [[ $# -eq 2 ]] || { echo 'usage: computer-decision.sh --ingest-traces TRACE_DIR' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_policy.py" ingest-traces "$2" --db "$policy_db"
    ;;
  --export-local)
    [[ $# -eq 1 ]] || { echo 'usage: computer-decision.sh --export-local' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_policy.py" export-local --db "$policy_db"
    ;;
  --train-local)
    [[ $# -eq 1 ]] || { echo 'usage: computer-decision.sh --train-local' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_model.py" train --db "$policy_db"
    ;;
  --compare-local)
    [[ $# -eq 1 ]] || { echo 'usage: computer-decision.sh --compare-local' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_model.py" compare --db "$policy_db"
    ;;
  --model-status)
    [[ $# -eq 1 ]] || { echo 'usage: computer-decision.sh --model-status' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_model.py" status --db "$policy_db"
    ;;
  --suggest-all)
    [[ $# -eq 2 ]] || { echo 'usage: computer-decision.sh --suggest-all REQUEST.json' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/offline_model.py" suggest "$2" --db "$policy_db"
    ;;
  --suggest-playbook)
    [[ $# -eq 3 ]] || { echo 'usage: computer-decision.sh --suggest-playbook PLAYBOOK.json STATE.json' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/playbook_decision.py" suggest "$2" "$3" --db "$policy_db"
    ;;
  --observe-playbook)
    [[ $# -eq 3 ]] || { echo 'usage: computer-decision.sh --observe-playbook PLAYBOOK.json EVENT.json' >&2; exit 2; }
    exec python3 -B "$repo_dir/tools/playbook_decision.py" observe "$2" "$3" --db "$policy_db"
    ;;
  *) echo 'usage: computer-decision.sh --self-test|--clef-fixture|--train FILE MODEL|--decide MODEL REQUEST' >&2; exit 2 ;;
esac
