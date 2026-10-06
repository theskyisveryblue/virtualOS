#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
POLLER_SCRIPT="$REPO_DIR/tools/cloudflare/poller.sh"
FLEET_PUSH="$REPO_DIR/tools/fleet/fleet_push.py"
WORKER_DIR="$REPO_DIR/tools/cloudflare/worker"
SCREEN_NAME="virtualos-poller"
LOG_FILE="/tmp/virtualOS-poller.log"
RELAY_ENV_FILE="$HOME/.config/virtualos/relay.env"
SUPERVISOR="${VOS_FLEET_SUPERVISOR:-http://127.0.0.1:9225}"

load_env() {
  if [[ -r "$RELAY_ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$RELAY_ENV_FILE"
  fi
  if [[ -z "${AUTH_TOKEN:-}" ]] && [[ -n "${VOS_AUTH_TOKEN:-}" ]]; then
    AUTH_TOKEN="$VOS_AUTH_TOKEN"
  fi
  STREAM_SERVER="${STREAM_SERVER:-http://127.0.0.1:8899}"
}

screen_session_exists() {
  (screen -ls 2>/dev/null || true) | grep -q "[.]${SCREEN_NAME}[[:space:]]"
}

screen_session_ids() {
  (screen -ls 2>/dev/null || true) | awk "/[.]${SCREEN_NAME}[[:space:]]/ { print \$1 }"
}

require_env() {
  load_env
  if [[ -z "${RELAY_URL:-}" ]] || [[ -z "${AUTH_TOKEN:-}" ]]; then
    echo "error: set RELAY_URL and AUTH_TOKEN in $RELAY_ENV_FILE" >&2
    exit 1
  fi
}

check_relay() {
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 5 "$RELAY_URL/health" 2>/dev/null | jq -e '.ok == true' >/dev/null &&
      curl -fsS --max-time 5 "$RELAY_URL/status" \
        -H "Authorization: Bearer $AUTH_TOKEN" 2>/dev/null | jq -e '.ok == true' >/dev/null; then
      return 0
    fi
    sleep 3
  done
  echo "error: relay health or authentication check failed at $RELAY_URL" >&2
  return 1
}

start_session() {
  require_env
  if screen_session_exists; then
    echo "relay poller already running in screen session '${SCREEN_NAME}'"
    return 0
  fi
  # The com.virtualos.stream LaunchAgent is the durable host loop; never stack a
  # second untagged poller on top of it or commands get consumed twice and
  # results race.
  if pgrep -f "fleet_push.py .*--watch --control" >/dev/null 2>&1; then
    echo "relay poller already running (com.virtualos.stream LaunchAgent)"
    return 0
  fi
  check_relay
  if [[ -f "$FLEET_PUSH" ]]; then
    # Fleet era: one loop both pushes fleet status/frames AND long-polls
    # /fleet/poll so a browser or agent can drive the guest's own input.
    # This mirrors the com.virtualos.stream LaunchAgent. VOS_POLLER_SESSION
    # tags this copy so stop/status can tell it apart from the agent's process.
    screen -dmS "$SCREEN_NAME" env \
      RELAY_URL="$RELAY_URL" \
      AUTH_TOKEN="$AUTH_TOKEN" \
      STREAM_SERVER="$STREAM_SERVER" \
      VOS_POLLER_SESSION="$SCREEN_NAME" \
      python3 "$FLEET_PUSH" --relay "$RELAY_URL" --supervisor "$SUPERVISOR" \
        --token "$AUTH_TOKEN" --interval 5 --watch --control --verbose \
      >>"$LOG_FILE" 2>&1
  else
    screen -dmS "$SCREEN_NAME" env \
      RELAY_URL="$RELAY_URL" \
      AUTH_TOKEN="$AUTH_TOKEN" \
      STREAM_SERVER="$STREAM_SERVER" \
      bash "$POLLER_SCRIPT" >>"$LOG_FILE" 2>&1
  fi
  sleep 1
  if screen_session_exists; then
    echo "relay poller started in detached screen session '${SCREEN_NAME}'"
    return 0
  fi
  echo "error: failed to start relay poller" >&2
  exit 1
}

stop_session() {
  local session_id
  while read -r session_id; do
    [[ -z "$session_id" ]] && continue
    screen -S "$session_id" -X quit >/dev/null 2>&1 || true
  done < <(screen_session_ids)
  pkill -f "$POLLER_SCRIPT" >/dev/null 2>&1 || true
  # Only kill the screen-launched copy; the com.virtualos.stream LaunchAgent
  # runs the same command line but is untagged and must survive.
  pkill -f "VOS_POLLER_SESSION=$SCREEN_NAME" >/dev/null 2>&1 || true
  echo "relay poller stopped"
}

status_session() {
  load_env
  if screen_session_exists; then
    echo "relay poller: running (screen session '${SCREEN_NAME}')"
  elif pgrep -f "fleet_push.py .*--watch --control" >/dev/null 2>&1; then
    echo "relay poller: running (com.virtualos.stream LaunchAgent)"
  else
    echo "relay poller: stopped"
  fi

  if [[ -n "${RELAY_URL:-}" ]] && [[ -n "${AUTH_TOKEN:-}" ]]; then
    local fleet
    fleet="$(curl -fsS --max-time 4 "$RELAY_URL/fleet/status" \
      -H "Authorization: Bearer $AUTH_TOKEN" 2>/dev/null || true)"
    if [[ -z "$fleet" ]]; then
      echo "relay endpoint: unreachable or unauthorized"
    else
      printf '%s' "$fleet" | python3 -c '
import json, sys
d = json.load(sys.stdin)
l = d.get("liveness", {})
print("relay fleet: live" if not l.get("isStale") else "relay fleet: STALE",
      "hb_age=%ss" % l.get("lastHeartbeatAgeSec"))
' 2>/dev/null || echo "relay endpoint: unexpected response"
    fi
  fi
}

logs_session() {
  tail -n 80 "$LOG_FILE"
}

load_cloudscope_ai_token() {
  local token
  command -v security >/dev/null || { echo 'error: macOS Keychain CLI is required' >&2; return 1; }
  token="$(security find-generic-password \
    -s com.terraform-ai.cloudscope.credential-vault.v4 -a connections -w 2>/dev/null |
    python3 -c '
import json, pathlib, plistlib, sys
root = pathlib.Path.home()
prefs = plistlib.loads((root / "Library/Preferences/com.terraform-ai.cloudscope.plist").read_bytes())
connections = json.loads(prefs["cloudscope.connections.v1"])
matches = [item for item in connections if "c24a3fb4bedba669785c18c834f51921" in item.get("accountIDs", [])]
if len(matches) != 1:
    raise SystemExit(1)
vault = json.load(sys.stdin)
token = vault.get(matches[0]["id"])
if not isinstance(token, str) or not token:
    raise SystemExit(1)
print(token)
')" || {
    echo 'error: CloudScope Ai credential is unavailable from Keychain' >&2; return 1;
  }
  if (( ${#token} < 30 || ${#token} > 256 )) || [[ ! "$token" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo 'error: CloudScope Ai credential has an unexpected format' >&2
    return 1
  fi
  export CLOUDFLARE_API_TOKEN="$token"
  export CLOUDFLARE_ACCOUNT_ID=c24a3fb4bedba669785c18c834f51921
  unset CF_API_TOKEN
}

prepare_cloud_disk_bucket() {
  local account_id=c24a3fb4bedba669785c18c834f51921
  local bucket=virtualos-cloud-disks
  local identity
  command -v wrangler >/dev/null || { echo 'cloud_disk_bucket=failed: Wrangler unavailable' >&2; return 1; }
  identity="$(cd "$WORKER_DIR" && wrangler whoami 2>&1)" || {
    echo 'cloud_disk_bucket=failed: Wrangler authentication unavailable' >&2; return 1;
  }
  if ! grep -Fq "$account_id" <<< "$identity" || grep -Fq 'harris@mergerealestate.ca' <<< "$identity"; then
    echo 'cloud_disk_bucket=refused: Ai account identity did not match' >&2
    return 1
  fi
  if (cd "$WORKER_DIR" && wrangler r2 bucket info "$bucket" >/dev/null 2>&1); then
    echo "cloud_disk_bucket=ready bucket=$bucket account_id=$account_id created=false"
    return 0
  fi
  (cd "$WORKER_DIR" && wrangler r2 bucket create "$bucket" --location enam --storage-class Standard >/dev/null) || {
    echo 'cloud_disk_bucket=failed: create was rejected' >&2; return 1;
  }
  (cd "$WORKER_DIR" && wrangler r2 bucket info "$bucket" >/dev/null 2>&1) || {
    echo 'cloud_disk_bucket=failed: created bucket did not verify' >&2; return 1;
  }
  echo "cloud_disk_bucket=ready bucket=$bucket account_id=$account_id created=true"
}

probe_cloud_disk_token_permission() {
  python3 - <<'PY'
import json, os, urllib.error, urllib.request
account = 'c24a3fb4bedba669785c18c834f51921'
url = f'https://api.cloudflare.com/client/v4/accounts/{account}/tokens/permission_groups'
request = urllib.request.Request(url, headers={
    'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN'],
    'Accept': 'application/json'})
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
except urllib.error.HTTPError as error:
    raise SystemExit(f'cloud_disk_token_probe=unavailable http_status={error.code}')
except (OSError, ValueError):
    raise SystemExit('cloud_disk_token_probe=unavailable network_or_response_error')
groups = payload.get('result', [])
matches = [group for group in groups if group.get('name') == 'Workers R2 Storage Bucket Item Read'
           and group.get('is_selectable') is True]
if payload.get('success') is not True or len(matches) != 1:
    raise SystemExit('cloud_disk_token_probe=unavailable read_group_not_selectable')
print('cloud_disk_token_probe=read_group_available id=' + matches[0]['id'])
PY
}

issue_cloud_disk_read_token() {
  local credential_file="$HOME/.config/virtualos/cloud-disk-r2-read.env"
  python3 - "$credential_file" <<'PY'
import datetime, hashlib, json, os, pathlib, re, sys, urllib.error, urllib.request

account = 'c24a3fb4bedba669785c18c834f51921'
bucket = 'virtualos-cloud-disks'
destination = pathlib.Path(sys.argv[1])
if destination.exists() or destination.is_symlink():
    raise SystemExit('cloud_disk_read_token=refused: destination already exists')
destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
if destination.parent.stat().st_mode & 0o077:
    raise SystemExit('cloud_disk_read_token=refused: private directory permissions are too broad')
base = f'https://api.cloudflare.com/client/v4/accounts/{account}/tokens'
headers = {'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN'],
           'Accept': 'application/json'}
def api(path, body=None):
    request = urllib.request.Request(base + path, headers={**headers,
        **({'Content-Type': 'application/json'} if body is not None else {})},
        data=json.dumps(body, separators=(',', ':')).encode() if body is not None else None,
        method='POST' if body is not None else 'GET')
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f'cloud_disk_read_token=failed http_status={error.code}')
    except (OSError, ValueError):
        raise SystemExit('cloud_disk_read_token=failed network_or_response_error')
    if result.get('success') is not True:
        raise SystemExit('cloud_disk_read_token=failed api_rejected')
    return result['result']

groups = api('/permission_groups')
matches = [group for group in groups if group.get('name') == 'Workers R2 Storage Bucket Item Read'
           and group.get('is_selectable') is True and re.fullmatch(r'[0-9a-f]{32}', str(group.get('id', '')))]
if len(matches) != 1:
    raise SystemExit('cloud_disk_read_token=failed read_group_unavailable')
expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
policy = {'effect': 'allow',
          'resources': {f'com.cloudflare.edge.r2.bucket.{account}_default_{bucket}': '*'},
          'permission_groups': [{'id': matches[0]['id']}]}
result = api('', {'name': 'virtualos-cloud-disk-read-mini-20261004',
                  'expires_on': expires, 'policies': [policy]})
access = result.get('id', '')
value = result.get('value', '')
if not re.fullmatch(r'[0-9a-f]{32}', access) or not isinstance(value, str) or not 40 <= len(value) <= 80:
    raise SystemExit('cloud_disk_read_token=failed: token response did not contain S3 credentials')
secret = hashlib.sha256(value.encode()).hexdigest()
contents = (f'CLOUDFLARE_ACCOUNT_ID={account}\n'
            f'CLOUDFLARE_R2_ENDPOINT=https://{account}.r2.cloudflarestorage.com\n'
            f'AWS_ACCESS_KEY_ID={access}\nAWS_SECRET_ACCESS_KEY={secret}\n')
descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
except BaseException:
    destination.unlink(missing_ok=True)
    raise
print(f'cloud_disk_read_token=created bucket={bucket} expires_on={expires} secret_printed=false')
PY
  ssh -o BatchMode=yes mac-mini-ai 'umask 077; mkdir -p "$HOME/.config/virtualos"; chmod 700 "$HOME/.config/virtualos"; target="$HOME/.config/virtualos/cloud-disk-r2-read.env"; if [[ -e "$target" || -L "$target" ]]; then exit 1; fi; cat > "$target"; chmod 600 "$target"' < "$credential_file"
  echo 'cloud_disk_read_token=installed_on_mini secret_printed=false'
}

deploy_relay() {
  # Run this on the operator Mac. The Mini only receives its host secret and
  # poller scripts; the browser secret stays in a private local file.
  local account_id=c24a3fb4bedba669785c18c834f51921
  local bucket=virtualos-remote-private
  local mini_host="${VOS_RELAY_MINI_HOST:-mac-mini-ai}"
  local mini_dir=/Users/ai/Documents/GitHub/virtualOS/tools/cloudflare
  local identity deployment relay_url host_token web_token
  command -v wrangler >/dev/null || { echo 'error: Wrangler is required' >&2; return 1; }
  command -v openssl >/dev/null || { echo 'error: OpenSSL is required' >&2; return 1; }
  command -v ssh >/dev/null && command -v scp >/dev/null || {
    echo 'error: SSH and SCP are required' >&2; return 1;
  }
  identity="$(cd "$WORKER_DIR" && wrangler whoami 2>&1)" || {
    echo 'error: Wrangler authentication failed' >&2; return 1;
  }
  if ! grep -Fq "$account_id" <<< "$identity" || grep -Fq 'harris@mergerealestate.ca' <<< "$identity"; then
    echo 'error: Wrangler must be logged in as ai@damidina.com; no deployment attempted' >&2
    return 1
  fi
  # `bucket list` returns only its first page; query the named bucket directly.
  if ! (cd "$WORKER_DIR" && wrangler r2 bucket info "$bucket" >/dev/null 2>&1); then
    (cd "$WORKER_DIR" && wrangler r2 bucket create "$bucket") || return 1
  fi
  deployment="$(cd "$WORKER_DIR" && wrangler deploy 2>&1)" || {
    printf '%s\n' "$deployment" >&2; return 1;
  }
  relay_url="$(grep -Eo 'https://virtualos-relay[[:alnum:].-]*\.workers\.dev' <<< "$deployment" | head -1 || true)"
  if [[ -z "$relay_url" ]]; then
    echo 'error: deployment succeeded but the Worker URL was not found' >&2
    return 1
  fi
  host_token="$(openssl rand -hex 32)" || return 1
  web_token="$(openssl rand -hex 32)" || return 1
  printf '%s' "$host_token" | (cd "$WORKER_DIR" && wrangler secret put AUTH_TOKEN) >/dev/null || return 1
  printf '%s' "$web_token" | (cd "$WORKER_DIR" && wrangler secret put WEB_TOKEN) >/dev/null || return 1
  umask 077
  mkdir -p "$HOME/.config/virtualos"
  printf '%s\n' "$web_token" > "$HOME/.config/virtualos/browser-token"
  scp "$POLLER_SCRIPT" "$REPO_DIR/tools/cloudflare/poller-session.sh" "$mini_host:$mini_dir/" || return 1
  printf 'RELAY_URL=%q\nAUTH_TOKEN=%q\n' "$relay_url" "$host_token" |
    ssh "$mini_host" 'umask 077; mkdir -p "$HOME/.config/virtualos"; cat > "$HOME/.config/virtualos/relay.env"' || return 1
  ssh "$mini_host" "$mini_dir/poller-session.sh restart" || return 1
  printf 'relay_deployed=%s\nbrowser_console=%s/console\nbrowser_token_file=%s\n' \
    "$relay_url" "$relay_url" "$HOME/.config/virtualos/browser-token"
}

clef_synthetic_smoke() {
  python3 -B - "$REPO_DIR/tools" <<'PY'
import json, math, os, sys, urllib.error, urllib.request
sys.path.insert(0, sys.argv[1])
from small_decider import clef_fixture

account = os.environ['CLOUDFLARE_ACCOUNT_ID']
url = f'https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/clef-flash'
payload = json.dumps(clef_fixture(), separators=(',', ':')).encode()
if len(payload) > 2048:
    raise SystemExit('clef_smoke=refused: synthetic request too large')
request = urllib.request.Request(url, data=payload, headers={
    'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN'],
    'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read(65537)
except urllib.error.HTTPError as error:
    raise SystemExit(f'clef_smoke=unavailable http_status={error.code}')
except OSError:
    raise SystemExit('clef_smoke=unavailable network_error')
if len(body) > 65536:
    raise SystemExit('clef_smoke=failed: oversized response')
result = json.loads(body)
if result.get('success') is not True or not isinstance(result.get('result'), dict):
    raise SystemExit('clef_smoke=unavailable: API rejected request')
answers = result['result'].get('answers', {})
answer = answers.get('next_action') if isinstance(answers, dict) else None
allowed = set(clef_fixture()['questions']['next_action']['criteria'])
if (not isinstance(answer, dict) or answer.get('type') != 'choice'
        or answer.get('choice') not in allowed
        or not isinstance(answer.get('probabilities'), dict)
        or set(answer['probabilities']) != allowed):
    raise SystemExit('clef_smoke=failed: invalid typed answer')
probabilities = answer['probabilities']
if (any(type(value) not in (int, float) or not math.isfinite(value)
        or not 0 <= value <= 1 for value in probabilities.values())
        or abs(sum(probabilities.values()) - 1) > 0.02):
    raise SystemExit('clef_smoke=failed: invalid probabilities')
print(json.dumps({'clef_smoke': 'passed', 'synthetic_only': True,
                  'choice': answer['choice'], 'probabilities': probabilities}, sort_keys=True))
PY
}

case "${1:-start}" in
  deploy-cloudscope) load_cloudscope_ai_token; deploy_relay ;;
  cloud-disk-bucket) load_cloudscope_ai_token; prepare_cloud_disk_bucket ;;
  cloud-disk-token-probe) load_cloudscope_ai_token; probe_cloud_disk_token_permission ;;
  cloud-disk-read-token) load_cloudscope_ai_token; issue_cloud_disk_read_token ;;
  clef-smoke) load_cloudscope_ai_token; clef_synthetic_smoke ;;
  deploy) deploy_relay ;;
  start) start_session ;;
  stop) stop_session ;;
  restart) stop_session; start_session ;;
  status) status_session ;;
  logs) logs_session ;;
  *)
    echo "usage: $(basename "$0") {deploy-cloudscope|cloud-disk-bucket|cloud-disk-token-probe|cloud-disk-read-token|clef-smoke|deploy|start|stop|restart|status|logs}" >&2
    exit 1
    ;;
esac
