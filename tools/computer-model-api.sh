#!/usr/bin/env bash
# Run the local model API. Supply the bearer token through VOS_MODEL_API_TOKEN.
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
state_dir="$HOME/Library/Application Support/virtualOS/model-api"
token_file="$state_dir/token"

case "${1:-}" in
  --start-mini)
    [[ $# -eq 1 ]] || { echo 'usage: computer-model-api.sh --start-mini' >&2; exit 2; }
    ifconfig bridge100 2>/dev/null | grep -Eq '^[[:space:]]+inet 192[.]168[.]64[.]1[[:space:]]' || {
      echo 'bridge100 must have 192.168.64.1 on the Mini' >&2; exit 2;
    }
    command -v screen >/dev/null || { echo 'screen is required' >&2; exit 2; }
    umask 077
    mkdir -p "$state_dir"
    chmod 700 "$state_dir"
    if [[ ! -f "$token_file" ]]; then
      openssl rand -hex 32 > "$token_file"
    fi
    chmod 600 "$token_file"
    if "$0" --status-mini >/dev/null 2>&1; then
      if grep '[.]virtualos-model-api' <<< "$(screen -ls 2>/dev/null || true)" >/dev/null; then
        echo 'virtualOS model API already running on 192.168.64.1:8765'
        exit 0
      fi
      echo 'model API is healthy but detached session is missing; inspect before restart' >&2
      exit 1
    fi
    screen -dmS virtualos-model-api "$0" --serve-mini
    for _ in 1 2 3 4 5; do
      if "$0" --status-mini >/dev/null 2>&1; then
        echo 'virtualOS model API running on 192.168.64.1:8765'
        exit 0
      fi
      sleep 1
    done
    echo 'model API did not start; inspect screen -r virtualos-model-api' >&2
    exit 1
    ;;
  --serve-mini)
    [[ $# -eq 1 && -f "$token_file" ]] || exit 2
    export VOS_MODEL_API_TOKEN="$(cat "$token_file")"
    [[ -x /opt/homebrew/bin/brotli ]] && export PATH="/opt/homebrew/bin:$PATH"
    bundle_args=()
    if [[ -f "$state_dir/models.bundle.br" ]]; then
      bundle_args=(--models-bundle "$state_dir/models.bundle.br")
    fi
    exec python3 -B "$repo_dir/tools/model_api.py" --host 192.168.64.1 --port 8765 \
      --allow-remote "${bundle_args[@]}"
    ;;
  --bundle-mini)
    [[ $# -eq 1 ]] || exit 2
    umask 077
    mkdir -p "$state_dir"
    python3 -B "$repo_dir/tools/build_model_bundle.py" --output "$state_dir/models.bundle.br"
    exit 0
    ;;
  --status-mini)
    [[ $# -eq 1 && -f "$token_file" ]] || exit 2
    exec python3 -B "$repo_dir/tools/model_api_status.py" "$token_file"
    ;;
  --self-test-mini)
    [[ $# -eq 1 && -f "$token_file" ]] || exit 2
    exec python3 -B "$repo_dir/tools/model_api_status.py" "$token_file" --full
    ;;
esac
exec python3 -B "$repo_dir/tools/model_api.py" "$@"
