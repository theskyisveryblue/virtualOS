#!/usr/bin/env bash
# poller.sh — runs on the Mac, polls the Cloudflare Worker for commands
# and executes them locally against virtualOS + the streaming server.
#
# Usage:
#   RELAY_URL=https://virtualos-relay.ai-c24.workers.dev \
#   VOS_RELAY_ENV_FILE="$HOME/.config/virtualos/relay.env" \
#   ./poller.sh
#
# Inactive compatibility path. Deploying this script also requires the matching
# tools/fleet/{relay_request,relay_read,fleet_push,guest_text}.py modules. The
# session deploy command packages these dependencies together.
#
# Commands the Worker can queue (via POST /cmd):
#   { "action": "start" }                    — launch virtualOS and start VM
#   { "action": "stop" }                     — quit virtualOS
#   { "action": "restart" }                  — stop + start
#   { "action": "screenshot" }               — capture screenshot, upload to R2
#   { "action": "type", "params": { "text": "hello" } }
#   { "action": "key", "params": { "key": "enter" } }
#   { "action": "shortcut", "params": { "key": "c", "modifiers": ["command"] } }
#   { "action": "click", "params": { "nx": 0.5, "ny": 0.5 } }
#   { "action": "focus", "params": { "app": "Terminal" } }
#   { "action": "status" }                   — report VM status
#   { "action": "password", "params": { "password": "..." } }

set +x
set -euo pipefail
umask 077

RELAY_URL="${RELAY_URL:-https://virtualos-relay.ai-c24.workers.dev}"
RELAY_ENV_FILE="${VOS_RELAY_ENV_FILE:-$HOME/.config/virtualos/relay.env}"
unset AUTH_TOKEN VOS_AUTH_TOKEN
STREAM_SERVER="${STREAM_SERVER:-http://127.0.0.1:8899}"
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
HEARTBEAT_INTERVAL=30
last_heartbeat=0

RELAY_REQUEST="$REPO_DIR/tools/fleet/relay_request.py"
relay_request() {
  python3 "$RELAY_REQUEST" --token-file "$RELAY_ENV_FILE" --relay "$RELAY_URL" "$@"
}

log() { echo "[$(date '+%H:%M:%S')] $*" >&2; }

# ── helpers ───────────────────────────────────────────────────────────

post_result() {
  local cmd_id="$1"
  shift
  local attempt
  for attempt in 1 2 3; do
    if printf '%s' "$1" | relay_request --method POST --path "/cmd/$cmd_id/result" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$attempt"
  done
  log "result delivery failed after three attempts: id=$cmd_id"
  return 0
}

post_usage_event() {
  printf '%s' "$1" | relay_request --method POST --path /usage >/dev/null 2>&1 || true
}

epoch_ms() {
  python3 -c 'import time; print(int(time.time() * 1000))'
}

capture_frame_to_file() {
  local outfile="$1"
  curl -q -fsS --noproxy '*' --proto '=http' --connect-timeout 2 \
    --max-time 10 --max-filesize 8388608 -o "$outfile" \
    "http://127.0.0.1:8845/vm/screen" 2>/dev/null
}

capture_usage_artifact() {
  local cmd_id="$1"
  local action="$2"
  local phase="$3"
  local outfile
  local name
  outfile="$(mktemp "/tmp/vos_trace_${cmd_id}_${phase}.XXXXXX")"
  name="${cmd_id}-${action}-${phase}.jpg"

  if ! capture_frame_to_file "$outfile"; then
    rm -f "$outfile"
    return 1
  fi

  if relay_request --method PUT --path "/usage/artifacts/$name" \
    --content-type image/jpeg < "$outfile" >/dev/null 2>&1
  then
    rm -f "$outfile"
    echo "usage/artifacts/$name"
    return 0
  fi

  rm -f "$outfile"
  return 1
}

traceable_action() {
  [[ "${VOS_RELAY_TRAINING_TRACE:-0}" == 1 ]] || return 1
  case "$1" in
    status|screenshot|frame|unlock|password) return 1 ;;
    *) return 0 ;;
  esac
}

record_command_trace() {
  local action="$1"
  local params_json="$2"
  local result_json="$3"
  local cmd_id="$4"
  local started_ms="$5"
  local before_key="${6:-}"
  local after_key="${7:-}"
  local payload

  payload=$(ACTION="$action" \
    PARAMS_JSON="$params_json" \
    RESULT_JSON="$result_json" \
    CMD_ID="$cmd_id" \
    STARTED_MS="$started_ms" \
    BEFORE_KEY="$before_key" \
    AFTER_KEY="$after_key" \
    python3 <<'PY'
import json
import os
import time

REDACTED = "[REDACTED]"
SENSITIVE_FIELDS = {
    "password",
    "passphrase",
    "secret",
    "token",
    "authorization",
    "cookie",
    "session",
    "api_key",
    "api-key",
    "apikey",
    "text",
    "cmd",
    "query",
}

def parse_json(value, fallback):
    try:
        return json.loads(value)
    except Exception:
        return fallback

def redact_sensitive(value):
    if isinstance(value, dict):
        return {
            key: (REDACTED if key.lower() in SENSITIVE_FIELDS else redact_sensitive(entry))
            for key, entry in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(entry) for entry in value]
    return value

started_ms = int(os.environ.get("STARTED_MS", "0") or "0")
finished_ms = int(time.time() * 1000)
payload = {
    "source": "relay-host",
    "kind": "command_trace",
    "action": os.environ.get("ACTION", ""),
    "commandId": os.environ.get("CMD_ID", ""),
    "metadata": {
        "params": redact_sensitive(parse_json(os.environ.get("PARAMS_JSON", "{}"), {})),
        "result": redact_sensitive(parse_json(os.environ.get("RESULT_JSON", "{}"), {})),
        "beforeArtifactKey": os.environ.get("BEFORE_KEY") or None,
        "afterArtifactKey": os.environ.get("AFTER_KEY") or None,
        "durationMs": max(finished_ms - started_ms, 0),
    },
}
print(json.dumps(payload))
PY
  ) || return 0

  post_usage_event "$payload"
}

vm_command_server_running() {
  curl -sf --max-time 1 "http://127.0.0.1:8845/vm/health" >/dev/null 2>&1
}

vm_command_post() {
  local path="$1"
  local payload="$2"
  printf '%s' "$payload" | curl -q -sf --noproxy '*' --proto '=http' \
    --connect-timeout 2 --max-time 10 -X POST "http://127.0.0.1:8845$path" \
    -H "Content-Type: application/json" --data-binary @-
}

json_quote() {
  printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

json_array() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@"
}

vm_click_payload() {
  local nx="$1"
  local ny="$2"
  python3 - "$nx" "$ny" <<'PY'
import json
import sys
import urllib.request

nx = float(sys.argv[1])
ny = float(sys.argv[2])

req = urllib.request.Request("http://127.0.0.1:8845/vm/screen", method="GET")
with urllib.request.urlopen(req, timeout=2.0) as resp:
    width = int(resp.headers.get("X-Capture-Width") or resp.headers.get("X-Image-Width") or "980")
    height = int(resp.headers.get("X-Capture-Height") or resp.headers.get("X-Image-Height") or "700")

nx = min(max(nx, 0.0), 1.0)
ny = min(max(ny, 0.0), 1.0)
x = round(nx * width)
y = round((1.0 - ny) * height)
print(json.dumps({"x": x, "y": y}))
PY
}

send_heartbeat() {
  local now
  now=$(date +%s)
  if (( now - last_heartbeat < HEARTBEAT_INTERVAL )); then return; fi
  last_heartbeat=$now

  local vm_state="unknown"
  local vm_name=""
  local window_count=0
  if vm_command_server_running; then
    vm_state="running"
    window_count=1
  elif pgrep -x virtualOS >/dev/null 2>&1; then
    vm_state="idle"
    window_count=1
  else
    vm_state="app_not_running"
  fi

  if [[ "$vm_state" == "running" ]]; then
    vm_name=$(python3 <<'PY'
import pathlib
import plistlib

defaults_path = pathlib.Path.home() / "Library/Containers/com.github.yep.ios.virtualOS/Data/Library/Preferences/com.github.yep.ios.virtualOS.plist"
name = ""
try:
    with defaults_path.open("rb") as fh:
        data = plistlib.load(fh)
    path = data.get("selectedVMBundlePath", "")
    if path:
        name = pathlib.Path(path).stem
except Exception:
    pass
print(name)
PY
)
  fi

  # Collect host metrics
  local cpu_load mem_free_mb disk_free_gb stream_ok vm_ip
  cpu_load=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}' || echo "0")
  mem_free_mb=$(vm_stat 2>/dev/null | awk '/Pages free/ {gsub(/\./,"",$3); print int($3*4096/1048576)}' || echo "0")
  disk_free_gb=$(df -g / 2>/dev/null | tail -1 | awk '{print $4}' || echo "0")

  # Check stream server
  stream_ok="false"
  if curl -sf --max-time 1 "$STREAM_SERVER/health" >/dev/null 2>&1; then
    stream_ok="true"
  fi

  # Check VM network (ping)
  vm_ip=""
  if [[ "$vm_state" == "running" ]]; then
    vm_ip=$(arp -a 2>/dev/null | grep "bridge100" | head -1 | awk -F'[()]' '{print $2}' || echo "")
  fi

  # Pull stream server status if available
  local vm_status_json="{}"
  if [[ "$stream_ok" == "true" ]]; then
    vm_status_json=$(curl -sf --max-time 2 "$STREAM_SERVER/api/vm/status" 2>/dev/null || echo "{}")
  fi

  # Build heartbeat payload
  local payload
  payload=$(VM_STATUS_JSON="$vm_status_json" VM_STATE="$vm_state" VM_NAME="$vm_name" \
    CPU_LOAD="$cpu_load" MEM_FREE_MB="$mem_free_mb" DISK_FREE_GB="$disk_free_gb" \
    STREAM_OK="$stream_ok" VM_IP="$vm_ip" WINDOW_COUNT="$window_count" \
    python3 <<'PY'
import json
import os
import time

def number(name, kind):
    try:
        return kind(os.environ.get(name, '0') or '0')
    except ValueError:
        return kind(0)

try:
    vm_status = json.loads(os.environ.get('VM_STATUS_JSON', '{}'))
except (TypeError, ValueError):
    vm_status = {}
bundle = vm_status.get('vmBundle') or {}
params = bundle.get('params') or {}
hb = {
    'state': os.environ['VM_STATE'],
    'vmName': os.environ.get('VM_NAME') or params.get('version', ''),
    'cpuCount': params.get('cpuCount', 0),
    'memoryGB': params.get('memorySizeInGB', 0),
    'host': {
        'cpuLoad': number('CPU_LOAD', float),
        'memFreeMB': number('MEM_FREE_MB', int),
        'diskFreeGB': number('DISK_FREE_GB', int),
    },
    'streamServer': os.environ.get('STREAM_OK') == 'true',
    'vmIP': os.environ.get('VM_IP', ''),
    'windowCount': number('WINDOW_COUNT', int),
    'diskAllocatedGB': bundle.get('diskAllocatedGB', 0),
    'diskLogicalGB': bundle.get('diskLogicalGB', 0),
    'monitorStatus': vm_status.get('monitorStatus', 'unknown'),
    'recentFailures': len(vm_status.get('recentFailureLines') or []),
    'ts': int(time.time()),
}
print(json.dumps(hb))
PY
  ) || payload="{\"state\":\"$vm_state\",\"ts\":$(date +%s)}"

  printf '%s' "$payload" | relay_request --method POST --path /status >/dev/null 2>&1 || true
}

# ── command handlers ──────────────────────────────────────────────────

handle_start() {
  log "CMD: start"
  local output
  if ! output=$("$REPO_DIR/start.sh" 2>&1); then
    log "start failed: ${output//$'\n'/; }"
    echo '{"ok":false,"action":"start","error":"startup failed; inspect host logs"}'
    return
  fi
  log "${output//$'\n'/; }"
  echo '{"ok":true,"action":"start"}'
}

handle_stop() {
  log "CMD: stop"
  "$REPO_DIR/tools/vosctl" stop >/dev/null 2>&1 || true
  echo '{"ok":true,"action":"stop"}'
}

handle_restart() {
  log "CMD: restart"
  local output
  if ! output=$("$REPO_DIR/tools/vosctl" restart 2>&1); then
    log "restart failed: ${output//$'\n'/; }"
    echo '{"ok":false,"action":"restart","error":"restart failed; inspect host logs"}'
    return
  fi
  log "${output//$'\n'/; }"
  echo '{"ok":true,"action":"restart"}'
}

handle_screenshot() {
  log "CMD: screenshot"
  local tmpfile
  tmpfile="$(mktemp /tmp/vos_screenshot.XXXXXX)" || { echo '{"ok":false,"error":"screenshot temp failed"}'; return; }
  if capture_frame_to_file "$tmpfile"; then
    if relay_request --method PUT --path /screenshot \
      --content-type image/jpeg < "$tmpfile" >/dev/null 2>&1; then
      rm -f "$tmpfile"
      echo '{"ok":true,"action":"screenshot","source":"vm_screen"}'
    else
      rm -f "$tmpfile"
      echo '{"ok":false,"error":"screenshot upload failed"}'
    fi
    return
  fi
  rm -f "$tmpfile"
  echo '{"ok":false,"error":"screenshot capture failed"}'
}

handle_frame() {
  local frame_file
  frame_file="$(mktemp /tmp/vos_guest_frame.XXXXXX)" || { echo '{"ok":false,"error":"frame temp failed"}'; return; }
  if ! capture_frame_to_file "$frame_file"; then
    rm -f "$frame_file"
    echo '{"ok":false,"error":"guest frame failed"}'
    return
  fi
  python3 - "$frame_file" <<'PY'
import base64, json, pathlib, sys
blob = pathlib.Path(sys.argv[1]).read_bytes()
if not 1000 <= len(blob) <= 1000000:
    print(json.dumps({'ok': False, 'error': 'invalid guest frame size'}))
else:
    print(json.dumps({'ok': True, 'data_url': 'data:image/jpeg;base64,' + base64.b64encode(blob).decode('ascii')}))
PY
  rm -f "$frame_file"
}

handle_unlock() {
  log "CMD: unlock"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  if ! "$REPO_DIR/tools/computer-wake.sh" >/dev/null 2>&1; then
    echo '{"ok":false,"error":"guest display could not be woken"}'
    return
  fi
  if "$REPO_DIR/tools/computer-unlock.sh" >/dev/null 2>&1; then
    echo '{"ok":true,"action":"unlock"}'
  else
    echo '{"ok":false,"error":"guest unlock was not confirmed"}'
  fi
}

handle_type() {
  local text="$1"
  log "CMD: type (${#text} characters)"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  vm_command_post "/vm/paste" "{\"text\":$(json_quote "$text")}" 2>&1 || echo '{"ok":false}'
}

handle_key() {
  local key="$1"
  log "CMD: key '$key'"
  local keycode
  case "$key" in
    enter|return) keycode=36 ;;
    escape|esc)   keycode=53 ;;
    tab)          keycode=48 ;;
    delete)       keycode=51 ;;
    up)           keycode=126 ;;
    down)         keycode=125 ;;
    left)         keycode=123 ;;
    right)        keycode=124 ;;
    space)        keycode=49 ;;
    *) log "  unknown key: $key"; echo "{\"ok\":false,\"error\":\"unknown key\"}"; return ;;
  esac
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  vm_command_post "/vm/key" "{\"key\":$(json_quote "$key")}" 2>&1 || echo '{"ok":false}'
}

handle_shortcut() {
  local key="$1"
  shift
  local mods=("$@")
  log "CMD: shortcut ${mods[*]}+$key"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  vm_command_post "/vm/shortcut" "{\"key\":$(json_quote "$key"),\"modifiers\":$(json_array "${mods[@]}")}" 2>&1 || echo '{"ok":false}'
}

handle_click() {
  local nx="$1" ny="$2"
  log "CMD: click nx=$nx ny=$ny"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  local payload
  payload=$(vm_click_payload "$nx" "$ny") || {
    echo '{"ok":false,"error":"failed to resolve vm click coordinates"}'
    return
  }
  vm_command_post "/vm/click" "$payload" 2>&1 || echo '{"ok":false}'
}

handle_focus() {
  local app="$1"
  log "CMD: focus '$app'"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  vm_command_post "/vm/open" "{\"app\":$(json_quote "$app")}" 2>&1 || echo '{"ok":false}'
}

handle_open() {
  local app="$1"
  local url="$2"
  local path_value="$3"
  log "CMD: open (app characters=${#app}, url characters=${#url}, path characters=${#path_value})"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  if [[ -n "$app" ]]; then
    vm_command_post "/vm/open" "{\"app\":$(json_quote "$app")}" 2>&1 || echo '{"ok":false}'
    return
  fi
  echo '{"ok":false,"error":"guest open currently supports app launches only"}'
}

handle_spotlight() {
  local query="$1"
  log "CMD: spotlight"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  vm_command_post "/vm/spotlight" "{\"query\":$(json_quote "$query")}" 2>&1 || echo '{"ok":false}'
}

handle_terminal_open() {
  log "CMD: terminal_open"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  vm_command_post "/vm/terminal/open" '{}' 2>&1 || echo '{"ok":false}'
}

handle_terminal_exec() {
  local command="$1"
  log "CMD: terminal_exec"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  vm_command_post "/vm/terminal/exec" "{\"cmd\":$(json_quote "$command"),\"newWindow\":true}" 2>&1 || echo '{"ok":false}'
}

handle_exec() {
  local command="$1"
  log "CMD: exec"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  vm_command_post "/vm/exec" "{\"cmd\":$(json_quote "$command"),\"paste\":true}" 2>&1 || echo '{"ok":false}'
}

handle_batch() {
  local params_json="$1"
  log "CMD: batch"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  local actions_json
  actions_json=$(echo "$params_json" | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin).get("actions", [])))' 2>/dev/null || echo "[]")
  vm_command_post "/vm/batch" "{\"actions\":$actions_json}" 2>&1 || echo '{"ok":false}'
}

handle_status() {
  log "CMD: status"
  local vm_state="unknown"
  if vm_command_server_running; then
    vm_state="running"
  elif pgrep -x virtualOS >/dev/null 2>&1; then
    vm_state="idle"
  else
    vm_state="app_not_running"
  fi
  echo "{\"ok\":true,\"state\":\"$vm_state\"}"
}

handle_password() {
  local pw="$1"
  log "CMD: password (entering)"
  if ! vm_command_server_running; then
    echo '{"ok":false,"error":"vm command server not running"}'
    return
  fi
  vm_command_post "/vm/unlock" "{\"password\":$(json_quote "$pw")}" 2>&1 || echo '{"ok":false}'
}

# ── main loop ─────────────────────────────────────────────────────────

log "virtualOS relay poller starting"
log "  RELAY_URL=$RELAY_URL"
log "  STREAM_SERVER=$STREAM_SERVER"

while true; do
  send_heartbeat

  # long-poll for next command (25s timeout on server side)
  response=$(relay_request --method GET --path /cmd/poll 2>/dev/null) || { sleep 2; continue; }

  # parse command
  cmd_id=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d.get('command'); print(c['id'] if c else '')" 2>/dev/null)
  if [[ -z "$cmd_id" ]]; then continue; fi

  action=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['command']['action'])" 2>/dev/null)
  params=$(echo "$response" | python3 -c "import sys,json; import json as j; print(j.dumps(json.load(sys.stdin)['command'].get('params',{})))" 2>/dev/null)

  log "Received command: id=$cmd_id action=$action"

  # dispatch
  started_ms=$(epoch_ms)
  before_key=""
  after_key=""
  result=""

  if traceable_action "$action"; then
    before_key=$(capture_usage_artifact "$cmd_id" "$action" "before" || true)
  fi

  case "$action" in
    start)      result=$(handle_start) ;;
    stop)       result=$(handle_stop) ;;
    restart)    result=$(handle_restart) ;;
    screenshot) result=$(handle_screenshot) ;;
    frame)      result=$(handle_frame) ;;
    unlock)     result=$(handle_unlock) ;;
    type)
      text=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('text',''))" 2>/dev/null)
      result=$(handle_type "$text")
      ;;
    key)
      key=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('key',''))" 2>/dev/null)
      result=$(handle_key "$key")
      ;;
    shortcut)
      key=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('key',''))" 2>/dev/null)
      IFS=' ' read -ra mods <<< "$(echo "$params" | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin).get('modifiers',[])))" 2>/dev/null)"
      result=$(handle_shortcut "$key" "${mods[@]}")
      ;;
    click)
      nx=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('nx',0.5))" 2>/dev/null)
      ny=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ny',0.5))" 2>/dev/null)
      result=$(handle_click "$nx" "$ny")
      ;;
    focus)
      app=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('app',''))" 2>/dev/null)
      result=$(handle_focus "$app")
      ;;
    open)
      app=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('app',''))" 2>/dev/null)
      url=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('url',''))" 2>/dev/null)
      path_value=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('path',''))" 2>/dev/null)
      result=$(handle_open "$app" "$url" "$path_value")
      ;;
    spotlight)
      query=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('query',''))" 2>/dev/null)
      result=$(handle_spotlight "$query")
      ;;
    terminal_open|terminal-open)
      result=$(handle_terminal_open)
      ;;
    terminal_exec|terminal-exec|terminal)
      command=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cmd',''))" 2>/dev/null)
      result=$(handle_terminal_exec "$command")
      ;;
    exec)
      command=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cmd',''))" 2>/dev/null)
      result=$(handle_exec "$command")
      ;;
    batch)
      result=$(handle_batch "$params")
      ;;
    status)     result=$(handle_status) ;;
    password)
      pw=$(echo "$params" | python3 -c "import sys,json; print(json.load(sys.stdin).get('password',''))" 2>/dev/null)
      if [[ -z "$pw" ]]; then log "  password action missing 'password' param"; result='{"ok":false,"error":"password param required"}'; post_result "$cmd_id" "$result"; continue; fi
      result=$(handle_password "$pw")
      ;;
    *)
      log "  unknown action: $action"
      result="{\"ok\":false,\"error\":\"unknown action: $action\"}"
      ;;
  esac

  if traceable_action "$action"; then
    after_key=$(capture_usage_artifact "$cmd_id" "$action" "after" || true)
    record_command_trace "$action" "$params" "$result" "$cmd_id" "$started_ms" "$before_key" "$after_key"
  fi

  post_result "$cmd_id" "$result"
done
