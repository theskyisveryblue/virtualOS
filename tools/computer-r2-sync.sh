#!/usr/bin/env bash
# Transfer reviewed guest examples and operate isolated VM backup buckets.
set -euo pipefail

if [[ "${1:-}" == cloud-disk-preflight && $# -eq 1 ]]; then
  script_dir="$(cd "$(dirname "$0")" && pwd)"
  config_file="${VOS_CLOUD_DISK_CONFIG:-$script_dir/cloudflare/cloud-disk.example.json}"
  credential_file="${VOS_CLOUD_DISK_CREDENTIAL_FILE:-}"
  [[ -n "$credential_file" && -r "$credential_file" ]] || {
    echo 'cloud_disk_preflight=pending: VOS_CLOUD_DISK_CREDENTIAL_FILE is unavailable' >&2
    exit 1
  }
  command -v aws >/dev/null || { echo 'cloud_disk_preflight=failed: AWS CLI unavailable' >&2; exit 1; }
  config_json="$(python3 "$script_dir/cloud_disk.py" check-config "$config_file")"
  read -r cloud_account cloud_bucket cloud_endpoint < <(printf '%s' "$config_json" | python3 -c \
    'import json,sys; x=json.load(sys.stdin); a=x["account_id"]; print(a,x["bucket"],"https://"+a+".r2.cloudflarestorage.com")')
  set +x
  unset CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_R2_ENDPOINT AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  set -a
  # shellcheck disable=SC1090
  source "$credential_file"
  set +a
  if [[ "${CLOUDFLARE_ACCOUNT_ID:-}" != "$cloud_account" ||
        "${CLOUDFLARE_R2_ENDPOINT:-}" != "$cloud_endpoint" ||
        -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    echo 'cloud_disk_preflight=failed: credentials do not match the pinned account config' >&2
    exit 1
  fi
  export AWS_DEFAULT_REGION=auto AWS_EC2_METADATA_DISABLED=true
  aws s3api head-bucket --bucket "$cloud_bucket" --endpoint-url "$cloud_endpoint" >/dev/null
  printf 'cloud_disk_preflight=passed account_id=%s bucket=%s writes=0\n' "$cloud_account" "$cloud_bucket"
  exit 0
fi

if [[ $# -ne 1 || ! "$1" =~ ^(status|archive|audit|prune|vm-probe|vm-backup|vm-backup-detached|vm-resume|vm-resume-detached|vm-monitor|vm-monitor-detached|vm-monitor-stop|vm-stop|vm-halt-preserve|vm-audit|vm-publish-manifest|vm-abort|vm-usage|vm-account-usage|vm-estimate|vm-speed|vm-range-probe|vm-inventory|vm-prune-probes)$ ]]; then
  echo 'Usage: ./tools/computer-r2-sync.sh cloud-disk-preflight|status|archive|audit|prune|vm-probe|vm-backup|vm-backup-detached|vm-resume|vm-resume-detached|vm-monitor|vm-monitor-detached|vm-monitor-stop|vm-stop|vm-halt-preserve|vm-audit|vm-publish-manifest|vm-abort|vm-usage|vm-account-usage|vm-estimate|vm-speed|vm-range-probe|vm-inventory|vm-prune-probes' >&2
  exit 2
fi

bucket=mac-mini-computer-use-backups
vm_bucket=virtualos-vm-backups
vm_receipt="$HOME/.config/virtualos/vm-r2-receipt.json"
vm_pending_receipt="$HOME/.config/virtualos/vm-r2-pending.json"
account_id=c24a3fb4bedba669785c18c834f51921
# The staged Mini tools live on local storage; its Documents checkout can block.
remote_script=/Users/ai/vos-fleet/tools/computer-r2.sh
local_script="$(cd "$(dirname "$0")" && pwd)/computer-r2.sh"
sync_script="$(cd "$(dirname "$0")" && pwd)/computer-r2-sync.sh"
mini_host="${VOS_VM_SSH_HOST:-mac-mini-ai}"
[[ "$mini_host" =~ ^[[:alnum:]][[:alnum:]_.@:-]*$ && ${#mini_host} -le 255 ]] || {
  echo 'vm_backup=refused: invalid VOS_VM_SSH_HOST' >&2; exit 2;
}
export VOS_VM_SSH_HOST="$mini_host"
vm_monitor_stop="$HOME/.config/virtualos/vm-backup-monitor-stop"
mode="$1"
credential_file="${VOS_R2_CREDENTIAL_FILE:-/Users/theone/Documents/GitHub/terraform-ai/cloudflare-credentials.local/terraform-ai-ai-inventory-2026-08-23.env}"

if ! command -v aws >/dev/null || ! command -v ssh >/dev/null || ! command -v scp >/dev/null; then
  echo 'AWS CLI, ssh and scp are required on this Mac' >&2
  exit 1
fi

if [[ ! -r "$credential_file" ]]; then
  echo "Missing Ai account R2 credentials: $credential_file" >&2
  exit 1
fi
# The file holds scoped S3 credentials. It is outside the repository and is
# never copied to the Mini or included in an archive.
# Never trace credential assignment, even if a caller starts this script with bash -x.
set +x
set -a
# shellcheck disable=SC1090
source "$credential_file"
set +a
if [[ "${CLOUDFLARE_ACCOUNT_ID:-}" != "$account_id" ||
      "${CLOUDFLARE_R2_ENDPOINT:-}" != "https://$account_id.r2.cloudflarestorage.com" ||
      -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
  echo 'R2 credentials are not pinned to the ai@damidina.com account' >&2
  exit 1
fi
export AWS_DEFAULT_REGION=auto AWS_EC2_METADATA_DISABLED=true
r2() { aws s3api "$@" --endpoint-url "$CLOUDFLARE_R2_ENDPOINT"; }

scratch="$(mktemp -d /private/tmp/vos-r2.XXXXXX)"
trap 'rm -rf "$scratch"' EXIT

audit_archive() {
  python3 - "$1" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile

path = Path(sys.argv[1])
if path.stat().st_size > 25_000_000:
    raise SystemExit('archive_audit=failed: archive is too large')
with tarfile.open(path, 'r:gz') as archive:
    members = archive.getmembers()
    names = [item.name for item in members]
    if len(names) != len(set(names)) or not all(item.isfile() for item in members):
        raise SystemExit('archive_audit=failed: duplicate or non-file member')
    allowed_image = re.compile(r'(dock|journeys)/frames/[0-9a-f]{32}(?:-after)?\.jpg')
    if any(name not in {'manifest.json', 'dataset.jsonl'} and not allowed_image.fullmatch(name)
           for name in names):
        raise SystemExit('archive_audit=failed: unexpected archive member')
    if names.count('manifest.json') != 1 or names.count('dataset.jsonl') != 1:
        raise SystemExit('archive_audit=failed: metadata missing')
    manifest = json.load(archive.extractfile('manifest.json'))
    rows = [json.loads(line) for line in archive.extractfile('dataset.jsonl') if line.strip()]
    allowed_keys = {'id', 'task', 'target_app', 'target_view', 'action_source',
                    'proposal_relation', 'image', 'image_sha256', 'image_width',
                    'image_height', 'action', 'outcome', 'source', 'after_image',
                    'after_sha256'}
    expected_images = set()
    ids = set()
    tasks = re.compile(r'Click (?:the (?:blue smiling Finder|black Terminal|blue Safari compass|multicolor Google Chrome) icon in the guest Dock|(?:Applications|Downloads|Documents|Desktop|Recents) in the Finder sidebar)')
    for row in rows:
        if (set(row) != allowed_keys or not re.fullmatch(r'[0-9a-f]{32}', row.get('id', ''))
                or row['id'] in ids or not tasks.fullmatch(row.get('task', ''))):
            raise SystemExit('archive_audit=failed: unapproved dataset field or task')
        dock_tasks = {
            'Finder': 'Click the blue smiling Finder icon in the guest Dock',
            'Terminal': 'Click the black Terminal icon in the guest Dock',
            'Safari': 'Click the blue Safari compass icon in the guest Dock',
            'Google Chrome': 'Click the multicolor Google Chrome icon in the guest Dock',
        }
        source = row.get('source')
        if source == 'dock':
            valid_task = (row.get('target_view') is None
                          and row.get('task') == dock_tasks.get(row.get('target_app'))
                          and row.get('action_source') == 'model_tooltip_guarded')
        else:
            view = row.get('target_view')
            valid_task = (source == 'journeys' and row.get('target_app') == 'Finder'
                          and view in {'Applications', 'Downloads', 'Documents', 'Desktop', 'Recents'}
                          and row.get('task') == f'Click {view} in the Finder sidebar'
                          and row.get('action_source') in {'model', 'ocr'})
        if (not valid_task
                or row.get('proposal_relation') not in {'model_point_executed', 'guard_corrected', 'no_valid_model_point'}
                or type(row.get('image_width')) is not int or not 1 <= row['image_width'] <= 8192
                or type(row.get('image_height')) is not int or not 1 <= row['image_height'] <= 8192):
            raise SystemExit('archive_audit=failed: unexpected task metadata')
        action = row.get('action')
        point = action.get('point_2d') if isinstance(action, dict) else None
        if (not isinstance(action, dict) or set(action) != {'type', 'point_2d'}
                or action.get('type') != 'click'
                or not isinstance(point, list) or len(point) != 2
                or any(type(value) not in (int, float) or not 0 <= value <= 1000 for value in point)
                or row.get('outcome') != 'visually_accepted_verified_click'
                or row.get('source') not in {'dock', 'journeys'}
                or row.get('target_app') not in {'Finder', 'Terminal', 'Safari', 'Google Chrome'}):
            raise SystemExit('archive_audit=failed: action or outcome is invalid')
        ids.add(row['id'])
        for image_key, hash_key, suffix in (('image', 'image_sha256', ''),
                                            ('after_image', 'after_sha256', '-after')):
            member_name = row.get(image_key, '')
            expected = f"{row.get('source')}/frames/{row['id']}{suffix}.jpg"
            if member_name != expected or not re.fullmatch(r'[0-9a-f]{64}', row.get(hash_key, '')):
                raise SystemExit('archive_audit=failed: image reference is invalid')
            blob = archive.extractfile(member_name).read()
            if hashlib.sha256(blob).hexdigest() != row[hash_key]:
                raise SystemExit('archive_audit=failed: image hash mismatch')
            expected_images.add(member_name)
    actual_images = set(names) - {'manifest.json', 'dataset.jsonl'}
    counts = {'dock': sum(row['source'] == 'dock' for row in rows),
              'journeys': sum(row['source'] == 'journeys' for row in rows)}
    if (actual_images != expected_images or manifest.get('format') != 'virtualos-reviewed-computer-use-v1'
            or manifest.get('counts') != counts
            or manifest.get('examples') != len(rows) or manifest.get('images') != len(actual_images)):
        raise SystemExit('archive_audit=failed: manifest or image set mismatch')
    print(json.dumps({'archive_audit': 'passed', 'examples': len(rows),
                      'images': len(actual_images), 'style_profile_included': False,
                      'unexpected_files': 0}, sort_keys=True))
PY
}

case "$mode" in
  vm-monitor-stop)
    mkdir -p "$(dirname "$vm_monitor_stop")"
    touch "$vm_monitor_stop"
    monitor_pid="$(pgrep -f '[c]omputer-r2-sync.sh vm-monitor$' | head -1 || true)"
    if [[ -n "$monitor_pid" ]]; then
      monitor_pgid="$(ps -p "$monitor_pid" -o pgid= | tr -d ' ')"
      backup_pid="$(pgrep -f '[c]omputer-r2-sync.sh (vm-backup|vm-resume)$' | head -1 || true)"
      backup_pgid=''
      if [[ -n "$backup_pid" ]]; then
        backup_pgid="$(ps -p "$backup_pid" -o pgid= | tr -d ' ')"
      fi
      own_pgid="$(ps -p "$$" -o pgid= | tr -d ' ')"
      [[ "$monitor_pgid" =~ ^[1-9][0-9]+$ &&
         "$monitor_pgid" != "$own_pgid" && "$monitor_pgid" != "$backup_pgid" ]] || {
        echo 'vm_monitor_stop=refused: cannot isolate monitor process group' >&2; exit 1;
      }
      kill -TERM -- "-$monitor_pgid"
    fi
    screen -S virtualos-vm-monitor -X quit >/dev/null 2>&1 || true
    echo 'vm_monitor_stop=complete: backup remains active'
    ;;
  vm-stop|vm-halt-preserve)
    mkdir -p "$(dirname "$vm_monitor_stop")"
    touch "$vm_monitor_stop"
    backup_pid="$(pgrep -f '[c]omputer-r2-sync.sh (vm-backup|vm-resume)$' | head -1 || true)"
    if [[ -n "$backup_pid" ]]; then
      backup_pgid="$(ps -p "$backup_pid" -o pgid= | tr -d ' ')"
      own_pgid="$(ps -p "$$" -o pgid= | tr -d ' ')"
      [[ "$backup_pgid" =~ ^[1-9][0-9]+$ && "$backup_pgid" != "$own_pgid" ]] || {
        echo 'vm_stop=refused: cannot isolate uploader process group' >&2; exit 1;
      }
      kill -TERM -- "-$backup_pgid"
      for (( attempt=0; attempt<15; attempt++ )); do
        if ! pgrep -f '[c]omputer-r2-sync.sh (vm-backup|vm-resume)$' >/dev/null 2>&1; then break; fi
        sleep 1
      done
      if pgrep -f '[c]omputer-r2-sync.sh (vm-backup|vm-resume)$' >/dev/null 2>&1; then
        echo 'vm_stop=failed: uploader did not stop' >&2; exit 1
      fi
    fi
    screen -S virtualos-vm-backup -X quit >/dev/null 2>&1 || true
    if [[ "$mode" == vm-halt-preserve ]]; then
      echo 'vm_halt_preserve=complete: uploader stopped; multipart and local clone retained'
      exit 0
    fi
    "$sync_script" vm-abort
    drop_ok=0
    for (( attempt=0; attempt<5; attempt++ )); do
      if ssh -o BatchMode=yes -o ControlMaster=no -o ControlPath=none "$mini_host" \
        "$remote_script vm-clone-drop"; then drop_ok=1; break; fi
      sleep 5
    done
    (( drop_ok )) || { echo 'vm_stop=failed: clone remains on Mini' >&2; exit 1; }
    echo 'vm_stop=complete: uploader, multipart state, and clone released'
    ;;
  vm-estimate)
    python3 - "$HOME/.config/virtualos/r2-usage.jsonl" <<'PY'
import json, os, pathlib, subprocess, sys

path = pathlib.Path(sys.argv[1])
rows = []
for line in path.read_text().splitlines():
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if 'pending_uploaded_bytes' in row:
        rows.append(row)
if len(rows) < 2:
    raise SystemExit('vm_estimate=unavailable: need two usage samples')
latest = rows[-1]
earlier = next((row for row in rows if row['at'] >= latest['at'] - 1200), rows[0])
elapsed = latest['at'] - earlier['at']
increment = latest['pending_uploaded_bytes'] - earlier['pending_uploaded_bytes']
if elapsed <= 0 or increment <= 0:
    raise SystemExit('vm_estimate=unavailable: upload not advancing')
rate = increment / elapsed
logical = 32212254720
sample_ratio = 0.6756
source_offset = None
try:
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
        '-o', 'ControlMaster=no', '-o', 'ControlPath=none', os.environ['VOS_VM_SSH_HOST'],
        'lsof -nP -o -F pfon /Users/ai/VirtualOSMachines/.virtualOS-r2-staging.bundle/Disk.img'],
        capture_output=True, text=True, timeout=12)
    if result.returncode == 0:
        offsets = [int(line[1:], 16) for line in result.stdout.splitlines()
                   if line.startswith('o0x')]
        source_offset = max(offsets, default=None)
except (OSError, subprocess.TimeoutExpired, ValueError):
    pass
if source_offset and source_offset >= 1_073_741_824:
    observed_ratio = latest['pending_uploaded_bytes'] / source_offset
    target = int(logical * observed_ratio + 33579164)
else:
    observed_ratio = None
    target = int(logical * sample_ratio + 33579164)
remaining = max(0, target - latest['pending_uploaded_bytes'])
print(json.dumps({'sample_gzip_ratio': sample_ratio,
                  'observed_stream_ratio': round(observed_ratio, 4) if observed_ratio else None,
                  'source_disk_read_gib': round(source_offset / 2**30, 2) if source_offset else None,
                  'rate_mib_per_second': round(rate / 2**20, 3),
                  'uploaded_gib': round(latest['pending_uploaded_bytes'] / 2**30, 2),
                  'estimated_total_gib': round(target / 2**30, 2),
                  'estimated_hours_remaining': round(remaining / rate / 3600, 1),
                  'estimate_is_sample_based': True}, sort_keys=True))
PY
    ;;
  vm-monitor-detached)
    command -v screen >/dev/null || { echo 'vm_monitor=failed: screen unavailable' >&2; exit 1; }
    if pgrep -f '[c]omputer-r2-sync.sh vm-monitor$' >/dev/null 2>&1; then
      echo 'vm_monitor=refused: monitor already running' >&2; exit 1
    fi
    monitor_log="$HOME/Library/Logs/virtualos-vm-monitor.log"
    mkdir -p "$(dirname "$vm_monitor_stop")"
    python3 - "$vm_monitor_stop" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[1]).unlink(missing_ok=True)
PY
    screen -dmS virtualos-vm-monitor bash -c '"$1" vm-monitor >> "$2" 2>&1' _ "$sync_script" "$monitor_log"
    for (( attempt=0; attempt<10; attempt++ )); do
      if pgrep -f '[c]omputer-r2-sync.sh vm-monitor$' >/dev/null 2>&1; then
        echo "vm_monitor=running_detached log=$monitor_log"
        exit 0
      fi
      sleep 1
    done
    echo "vm_monitor=failed: session exited; log=$monitor_log" >&2
    exit 1
    ;;
  vm-monitor)
    interval="${VOS_VM_MONITOR_INTERVAL:-300}"
    hours="${VOS_VM_MONITOR_HOURS:-16}"
    [[ "$interval" =~ ^[1-9][0-9]*$ && "$hours" =~ ^[1-9][0-9]*$ ]] || {
      echo 'vm_monitor=failed: invalid interval or duration' >&2; exit 2;
    }
    end_at="$(( $(date +%s) + hours * 3600 ))"
    cycle=0
    recovery_restarts=0
    max_restarts="${VOS_VM_MONITOR_RESTARTS:-5}"
    [[ "$max_restarts" =~ ^[1-9][0-9]*$ ]] || {
      echo 'vm_monitor=failed: invalid recovery restart limit' >&2; exit 2;
    }
    while (( $(date +%s) < end_at )); do
      if [[ -e "$vm_monitor_stop" ]]; then
        echo 'vm_monitor=stopped: explicit stop marker'
        exit 0
      fi
      printf 'vm_monitor_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      "$sync_script" vm-usage || echo 'vm_monitor=usage_query_failed'
      if (( cycle % 12 == 0 )); then
        "$sync_script" vm-account-usage || echo 'vm_monitor=account_query_failed'
      fi
      free_kib="$(ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 \
        -o ServerAliveCountMax=2 -o ControlMaster=no -o ControlPath=none "$mini_host" \
        'df -Pk /System/Volumes/Data | awk '\''NR==2 {print $4}'\''' 2>/dev/null)" \
        || free_kib=''
      printf 'vm_host_free_kib=%s\n' "${free_kib:-unknown}"
      if [[ "$free_kib" =~ ^[0-9]+$ ]] && (( free_kib < 10485760 )); then
        echo 'vm_monitor=low_disk_halt: stopping upload and preserving multipart and clone'
        "$sync_script" vm-halt-preserve || echo 'vm_monitor=stop_failed'
        exit 1
      fi
      if pgrep -f '[c]omputer-r2-sync.sh (vm-backup|vm-resume)$' >/dev/null; then
        echo 'vm_backup_state=running'
      elif [[ -r "$vm_receipt" ]]; then
        echo 'vm_backup_state=finished_receipt_present'
        "$sync_script" vm-publish-manifest
        exit 0
      else
        echo 'vm_backup_state=stopped_without_receipt'
        if ! python3 "$(dirname "$local_script")/vm_backup_recovery.py" can-retry \
          "$HOME/Library/Logs/virtualos-vm-backup.log"; then
          echo 'vm_monitor=recovery_refused: uploader reported an integrity failure'
          exit 1
        fi
        probe_status=0
        python3 "$(dirname "$local_script")/vm_backup_recovery.py" probe "$mini_host" || probe_status=$?
        if (( probe_status == 75 )); then
          echo 'vm_monitor=waiting_for_ssh: multipart and clone retained'
        elif (( probe_status != 0 )); then
          echo 'vm_monitor=recovery_refused: clone or resource gate failed'
          exit 1
        elif (( recovery_restarts >= max_restarts )); then
          echo 'vm_monitor=recovery_refused: bounded restart limit reached'
          exit 1
        else
          [[ ! -e "$vm_monitor_stop" ]] || { echo 'vm_monitor=stopped: explicit stop marker'; exit 0; }
          recovery_restarts=$((recovery_restarts + 1))
          "$sync_script" vm-resume-detached || echo 'vm_monitor=resume_start_failed'
        fi
      fi
      cycle=$((cycle + 1))
      sleep "$interval"
    done
    echo 'vm_monitor=finished_bounded_window'
    ;;
  vm-account-usage)
    [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]] || { echo 'vm_account_usage=unavailable: API token absent' >&2; exit 1; }
    python3 - "$account_id" "$vm_bucket" "$HOME/.config/virtualos/r2-usage.jsonl" <<'PY'
import datetime as dt, json, os, pathlib, sys, urllib.request

account, bucket, log_path = sys.argv[1:]
now = dt.datetime.now(dt.timezone.utc)
start = (now - dt.timedelta(days=1)).isoformat().replace('+00:00', 'Z')
month = (now - dt.timedelta(days=30)).isoformat().replace('+00:00', 'Z')
query = '''query($accountTag: string!, $startDate: Time, $monthDate: Time, $bucketName: string) {
 viewer { accounts(filter: {accountTag: $accountTag}) {
  r2StorageAdaptiveGroups(limit: 10000, filter: {datetime_geq: $startDate}, orderBy: [datetime_DESC]) {
   max { payloadSize metadataSize objectCount uploadCount }
   dimensions { bucketName datetime }
  }
  r2OperationsAdaptiveGroups(limit: 10000,
   filter: {datetime_geq: $monthDate, bucketName: $bucketName}) {
   sum { requests }
   dimensions { actionType }
  }
  accountOperations: r2OperationsAdaptiveGroups(limit: 10000,
   filter: {datetime_geq: $monthDate}) {
   sum { requests }
   dimensions { actionType }
  }
  r2BandwidthUsageAdaptiveGroups(limit: 1000,
   filter: {datetime_geq: $startDate, bucketName: $bucketName}) {
   sum { bytesUpload bytesDownload }
  }
 } } }'''
payload = json.dumps({'query': query, 'variables': {'accountTag': account,
    'startDate': start, 'monthDate': month, 'bucketName': bucket}}).encode()
request = urllib.request.Request('https://api.cloudflare.com/client/v4/graphql', data=payload,
    headers={'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN'],
             'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=20) as response:
    result = json.load(response)
if result.get('errors'):
    raise SystemExit('vm_account_usage=failed: GraphQL rejected query')
accounts = result.get('data', {}).get('viewer', {}).get('accounts', [])
if len(accounts) != 1:
    raise SystemExit('vm_account_usage=failed: account analytics unavailable')
data = accounts[0]
bucket_request = urllib.request.Request(
    f'https://api.cloudflare.com/client/v4/accounts/{account}/r2/buckets/{bucket}',
    headers={'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN']})
with urllib.request.urlopen(bucket_request, timeout=20) as response:
    bucket_result = json.load(response)
bucket_info = bucket_result.get('result') or {}
def bucket_get(suffix):
    request = urllib.request.Request(
        f'https://api.cloudflare.com/client/v4/accounts/{account}/r2/buckets/{bucket}/{suffix}',
        headers={'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN']})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response).get('result') or {}
managed = bucket_get('domains/managed')
custom = bucket_get('domains/custom')
latest = {}
for group in data.get('r2StorageAdaptiveGroups', []):
    name = group['dimensions'].get('bucketName')
    if name and name not in latest:
        latest[name] = group['max']
stored = sum(item.get('payloadSize', 0) or 0 for item in latest.values())
operations = {}
for group in data.get('r2OperationsAdaptiveGroups', []):
    action = group['dimensions'].get('actionType', 'unknown')
    operations[action] = operations.get(action, 0) + (group['sum'].get('requests', 0) or 0)
account_operations = {}
for group in data.get('accountOperations', []):
    action = group['dimensions'].get('actionType', 'unknown')
    account_operations[action] = account_operations.get(action, 0) + (group['sum'].get('requests', 0) or 0)
class_a = {'ListBuckets', 'PutBucket', 'ListObjects', 'PutObject', 'CopyObject',
           'CompleteMultipartUpload', 'CreateMultipartUpload', 'LifecycleStorageTierTransition',
           'ListMultipartUploads', 'UploadPart', 'UploadPartCopy', 'ListParts',
           'PutBucketEncryption', 'PutBucketCors', 'PutBucketLifecycleConfiguration'}
class_b = {'HeadBucket', 'HeadObject', 'GetObject', 'UsageSummary',
           'GetBucketEncryption', 'GetBucketLocation', 'GetBucketCors',
           'GetBucketLifecycleConfiguration'}
bandwidth = data.get('r2BandwidthUsageAdaptiveGroups', [])
row = {'at': int(now.timestamp()), 'account_r2_buckets_seen': len(latest),
       'account_r2_stored_bytes_latest': stored,
       'account_r2_stored_gb_decimal_latest': round(stored / 1e9, 3),
       'vm_bucket_location': bucket_info.get('location'),
       'vm_bucket_jurisdiction': bucket_info.get('jurisdiction'),
       'vm_bucket_storage_class': bucket_info.get('storage_class'),
       'vm_bucket_r2_dev_enabled': managed.get('enabled'),
       'vm_bucket_enabled_custom_domains': sum(bool(item.get('enabled')) for item in custom.get('domains', [])),
       'vm_bucket_operations_30d': operations,
       'vm_bucket_uploaded_bytes_24h_reported': sum((g['sum'].get('bytesUpload') or 0) for g in bandwidth),
       'vm_bucket_downloaded_bytes_24h_reported': sum((g['sum'].get('bytesDownload') or 0) for g in bandwidth),
       'account_r2_class_a_operations_30d': sum(account_operations.get(action, 0) for action in class_a),
       'account_r2_class_b_operations_30d': sum(account_operations.get(action, 0) for action in class_b),
       'account_r2_class_a_free_allowance_month': 1000000,
       'account_r2_class_b_free_allowance_month': 10000000,
       'account_r2_operations_30d': account_operations,
       'storage_snapshot_hours': 24,
       'bandwidth_excludes_transfers_below_bytes': 102400}
path = pathlib.Path(log_path)
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
with path.open('a') as stream:
    os.chmod(path, 0o600)
    stream.write(json.dumps(row, sort_keys=True) + '\n')
print(json.dumps(row, sort_keys=True))
PY
    ;;
  vm-publish-manifest)
    [[ -r "$vm_receipt" ]] || { echo 'vm_manifest=refused: verified receipt missing' >&2; exit 1; }
    read -r key size < <(python3 - "$vm_receipt" "$vm_bucket" <<'PY'
import json, re, sys
r = json.load(open(sys.argv[1]))
key = r.get('key', '')
if (r.get('bucket') != sys.argv[2] or
    not re.fullmatch(r'vm-backups/[0-9]{8}T[0-9]{6}Z-[0-9]+/virtualOS\.bundle\.tar\.gz', key) or
    not re.fullmatch(r'[0-9a-f]{64}', r.get('sha256', '')) or
    type(r.get('bytes')) is not int or r['bytes'] < 1024 * 1024):
    raise SystemExit('vm_manifest=refused: invalid receipt')
print(key, r['bytes'])
PY
)
    actual_size="$(r2 head-object --bucket "$vm_bucket" --key "$key" --query ContentLength --output text)"
    [[ "$actual_size" == "$size" ]] || { echo 'vm_manifest=failed: object size differs from receipt' >&2; exit 1; }
    manifest_key="${key%.tar.gz}.receipt.json"
    r2 put-object --bucket "$vm_bucket" --key "$manifest_key" \
      --body "$vm_receipt" --content-type application/json >/dev/null
    r2 get-object --bucket "$vm_bucket" --key "$manifest_key" "$scratch/manifest.json" >/dev/null
    cmp "$vm_receipt" "$scratch/manifest.json"
    echo "vm_manifest=published_and_checked key=$manifest_key"
    ;;
  vm-backup-detached|vm-resume-detached)
    command -v screen >/dev/null || { echo 'vm_backup=failed: screen unavailable' >&2; exit 1; }
    if pgrep -f '[c]omputer-r2-sync.sh (vm-backup|vm-resume)$' >/dev/null 2>&1; then
      echo 'vm_backup=refused: detached backup already running' >&2
      exit 1
    fi
    log="$HOME/Library/Logs/virtualos-vm-backup.log"
    backup_mode=vm-backup
    [[ "$mode" == vm-resume-detached ]] && backup_mode=vm-resume
    mkdir -p "$(dirname "$log")"
    screen -dmS virtualos-vm-backup bash -c '"$1" "$3" >> "$2" 2>&1' _ "$sync_script" "$log" "$backup_mode"
    started=0
    for (( attempt=0; attempt<10; attempt++ )); do
      if pgrep -f '[c]omputer-r2-sync.sh (vm-backup|vm-resume)$' >/dev/null 2>&1; then
        started=1
        break
      fi
      sleep 1
    done
    (( started )) || {
      echo "vm_backup=failed: detached job exited; log=$log" >&2
      exit 1
    }
    echo "vm_backup=running_detached log=$log"
    ;;
  vm-speed)
    for host in operator mini; do
      if [[ "$host" == operator ]]; then
        rate="$(dd if=/dev/zero bs=1048576 count=5 2>/dev/null | curl -fsS --max-time 30 -o /dev/null -w '%{speed_upload}' -X POST --data-binary @- 'https://speed.cloudflare.com/__up?bytes=5242880')"
      else
        rate="$(ssh -o ControlMaster=no -o ControlPath=none mac-mini-ai 'dd if=/dev/zero bs=1048576 count=5 2>/dev/null | curl -fsS --max-time 30 -o /dev/null -w "%{speed_upload}" -X POST --data-binary @- "https://speed.cloudflare.com/__up?bytes=5242880"')"
      fi
      printf 'vm_speed host=%s upload_bytes_per_second=%s\n' "$host" "$rate"
    done
    ;;
  vm-range-probe)
    # Read a few small byte ranges from an existing private object. This
    # measures the current CLI path, not an optimized persistent NBD cache.
    python3 - "$vm_bucket" "$CLOUDFLARE_R2_ENDPOINT" "$scratch" <<'PY'
import json, pathlib, statistics, subprocess, sys, time

bucket, endpoint, scratch = sys.argv[1:]
base = ['aws', 's3api']
listed = subprocess.run(base + ['list-objects-v2', '--bucket', bucket,
                                '--endpoint-url', endpoint],
                        check=True, capture_output=True, text=True)
objects = json.loads(listed.stdout).get('Contents', [])
item = next((obj for obj in objects if obj.get('Size', 0) >= 1048576), None)
if item is None:
    raise SystemExit('vm_range_probe=unavailable: no completed private object >= 1 MiB')
sizes = (65536, 65536, 65536, 1048576)
samples = []
for index, size in enumerate(sizes):
    target = pathlib.Path(scratch) / f'range-{index}.bin'
    started = time.monotonic()
    result = subprocess.run(base + ['get-object', '--bucket', bucket,
        '--key', item['Key'], '--range', f'bytes=0-{size - 1}',
        str(target), '--endpoint-url', endpoint],
        capture_output=True, text=True)
    elapsed = time.monotonic() - started
    if result.returncode or target.stat().st_size != size:
        raise SystemExit(f'vm_range_probe=failed: size={size} exit={result.returncode} '
                         f'error={result.stderr[-250:]}')
    samples.append({'bytes': size, 'seconds': round(elapsed, 3)})
print(json.dumps({'vm_range_probe': 'passed', 'method': 'aws_s3api_get_object',
                  'samples': samples,
                  'median_64k_seconds': round(statistics.median(s['seconds'] for s in samples[:3]), 3),
                  'writes': 0, 'reads': len(samples),
                  'scope': 'CLI end-to-end latency, not a VM boot benchmark'}, sort_keys=True))
PY
    ;;
  vm-inventory)
    python3 - "$vm_bucket" "$CLOUDFLARE_R2_ENDPOINT" <<'PY'
import json, subprocess, sys
bucket, endpoint = sys.argv[1:]
result = subprocess.run(['aws', 's3api', 'list-objects-v2', '--bucket', bucket,
                         '--endpoint-url', endpoint],
                        check=True, capture_output=True, text=True)
objects = json.loads(result.stdout).get('Contents', [])
print(json.dumps({'vm_inventory': [{'key': item['Key'], 'bytes': item['Size'],
                                    'modified': item['LastModified']} for item in objects],
                  'count': len(objects)}, sort_keys=True))
PY
    ;;
  vm-prune-probes)
    # Only two known disposable August probes, never a backup or its metadata.
    for key in probe.txt virtualOS_3/20260303-202707/Disk.img.test64m; do
      if r2 head-object --bucket "$vm_bucket" --key "$key" >/dev/null 2>&1; then
        r2 delete-object --bucket "$vm_bucket" --key "$key" >/dev/null
        echo "vm_prune_probes=deleted key=$key"
      fi
    done
    ;;
  vm-usage)
    python3 - "$vm_bucket" "$CLOUDFLARE_R2_ENDPOINT" "$HOME/.config/virtualos/r2-usage.jsonl" <<'PY'
import json, os, pathlib, subprocess, sys, time

bucket, endpoint, log_path = sys.argv[1:]
def call(*args):
    command = ['aws', 's3api', *args, '--bucket', bucket, '--endpoint-url', endpoint,
               '--cli-connect-timeout', '5', '--cli-read-timeout', '10']
    failure = 'unknown'
    for attempt in range(2):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=18)
            if result.returncode == 0:
                return json.loads(result.stdout)
            failure = f'status={result.returncode} error={result.stderr[-300:]}'
        except subprocess.TimeoutExpired:
            failure = 'timeout_after_18_seconds'
        if attempt == 0:
            time.sleep(1)
    raise SystemExit(f'vm_usage=failed operation={args[0]} {failure}')

objects = call('list-objects-v2').get('Contents', [])
pending = call('list-multipart-uploads').get('Uploads', [])
parts = 0
pending_bytes = 0
for item in pending:
    if not item.get('Key', '').startswith('vm-backups/'):
        continue
    response = call('list-parts', '--key', item['Key'], '--upload-id', item['UploadId'])
    listed = response.get('Parts', [])
    parts += len(listed)
    pending_bytes += sum(part['Size'] for part in listed)
stored_bytes = sum(item['Size'] for item in objects)
billable_candidate_bytes = stored_bytes + pending_bytes
row = {'at': int(time.time()), 'bucket': bucket, 'stored_objects': len(objects),
       'stored_bytes': stored_bytes, 'pending_parts': parts,
       'pending_uploaded_bytes': pending_bytes,
       'stored_plus_pending_bytes': billable_candidate_bytes,
       'stored_gb_decimal': round(stored_bytes / 1e9, 3),
       'gross_storage_usd_per_30d_if_held': round(billable_candidate_bytes / 1e9 * .015, 3),
       'account_free_gb_month_shared': 10}
path = pathlib.Path(log_path)
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
with path.open('a') as stream:
    os.chmod(path, 0o600)
    stream.write(json.dumps(row, sort_keys=True) + '\n')
print(json.dumps(row, sort_keys=True))
PY
    ;;
  vm-abort)
    python3 - "$vm_bucket" "$CLOUDFLARE_R2_ENDPOINT" <<'PY'
import json, subprocess, sys

bucket, endpoint = sys.argv[1:]
base = ['aws', 's3api']
result = subprocess.run(base + ['list-multipart-uploads', '--bucket', bucket,
                                '--endpoint-url', endpoint], check=True,
                        capture_output=True, text=True)
uploads = json.loads(result.stdout).get('Uploads', [])
count = 0
for item in uploads:
    key = item.get('Key', '')
    if not key.startswith('vm-backups/') or not key.endswith('/virtualOS.bundle.tar.gz'):
        continue
    subprocess.run(base + ['abort-multipart-upload', '--bucket', bucket,
                           '--key', key, '--upload-id', item['UploadId'],
                           '--endpoint-url', endpoint], check=True, capture_output=True)
    count += 1
print(f'vm_abort=passed aborted_uploads={count}')
PY
    ;;
  vm-backup|vm-resume|vm-audit)
    if ! r2 head-bucket --bucket "$vm_bucket" >/dev/null; then
      echo "vm_backup=failed: private bucket $vm_bucket is unavailable" >&2
      exit 1
    fi
    if [[ "$mode" == vm-backup ]]; then
      echo 'vm_backup=preparing_consistent_clone'
      scp -o ControlMaster=no -o ControlPath=none "$local_script" "$mini_host:$remote_script"
      ssh -o ControlMaster=no -o ControlPath=none "$mini_host" "$remote_script vm-clone"
      # Preserve the local stopped-disk clone on any upload, audit, or VM
      # restart failure. It is the only recovery copy until R2 is verified.
      vm_ready=0
      for (( attempt=0; attempt<60; attempt++ )); do
        if ssh -o BatchMode=yes -o ControlMaster=no -o ControlPath=none "$mini_host" \
          'curl -fsS --max-time 3 http://127.0.0.1:8845/vm/health' \
          | python3 -c 'import json,sys; s=json.load(sys.stdin); sys.exit(0 if s.get("ready") and s.get("state")=="running" else 1)' 2>/dev/null; then
          vm_ready=1
          break
        fi
        sleep 1
      done
      (( vm_ready )) || { echo 'vm_backup=failed: guest did not restart after clone' >&2; exit 1; }
      echo 'vm_backup=guest_running_after_clone'
      key="vm-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$/virtualOS.bundle.tar.gz"
      python3 - "$key" "$vm_bucket" "$CLOUDFLARE_R2_ENDPOINT" "$scratch/upload.json" <<'PY'
import hashlib, json, os, subprocess, sys, time

key, bucket, endpoint, result_path = sys.argv[1:]
def check_host_space():
    result = subprocess.run([
        'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', os.environ['VOS_VM_SSH_HOST'],
        "df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}'"
    ], capture_output=True, text=True, timeout=15)
    try:
        free_kib = int(result.stdout.strip()) if result.returncode == 0 else 0
    except ValueError:
        free_kib = 0
    if free_kib < 10 * 1024 * 1024:
        raise RuntimeError('vm_backup=paused: Mini disk below 10 GiB or unavailable')

check_host_space()
source = subprocess.Popen([
    'ssh', '-o', 'BatchMode=yes', '-o', 'ControlMaster=no', '-o', 'ControlPath=none', os.environ['VOS_VM_SSH_HOST'],
    'bash -o pipefail -c \'tar -cf - -C "$HOME/VirtualOSMachines/.virtualOS-r2-staging.bundle" . | gzip -1 -n\''
], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
sink = subprocess.Popen([
    'aws', 's3', 'cp', '-', f's3://{bucket}/{key}',
    '--endpoint-url', endpoint, '--only-show-errors', '--no-progress',
    '--content-type', 'application/gzip'
], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
digest = hashlib.sha256()
size = 0
last_space_check = 0
started = time.monotonic()
try:
    while chunk := source.stdout.read(1024 * 1024):
        sink.stdin.write(chunk)
        digest.update(chunk)
        size += len(chunk)
        if size - last_space_check >= 512 * 1024**2:
            check_host_space()
            last_space_check = size
        if size and size % (1024 * 1024 * 1024) < len(chunk):
            print(f'vm_backup=uploading compressed_gib={size / 2**30:.1f}', flush=True)
    sink.stdin.close()
    source_code = source.wait()
    sink_code = sink.wait()
except BaseException:
    if source.poll() is None:
        source.kill()
    if sink.poll() is None:
        sink.kill()
    source.wait(timeout=10)
    sink.wait(timeout=10)
    raise
if source_code or sink_code or size < 1024 * 1024:
    raise SystemExit(f'vm_backup=upload_failed source={source_code} sink={sink_code} '
                     f'source_error={source.stderr.read().decode(errors="replace")[-1000:]} '
                     f'sink_error={sink.stderr.read().decode(errors="replace")[-1000:]}')
with open(result_path, 'w') as stream:
    json.dump({'bucket': bucket, 'key': key, 'sha256': digest.hexdigest(),
               'bytes': size, 'upload_seconds': round(time.monotonic() - started, 1)}, stream)
print(f'vm_backup=uploaded compressed_gib={size / 2**30:.2f} sha256={digest.hexdigest()}')
PY
      cp "$scratch/upload.json" "$scratch/pending-receipt.json"
    elif [[ "$mode" == vm-resume ]]; then
      mkdir -p "$(dirname "$vm_pending_receipt")"
      chmod 700 "$(dirname "$vm_pending_receipt")"
      # Recover a completion that was interrupted before its full GET audit.
      completed_pending=0
      if [[ -r "$vm_pending_receipt" ]]; then
        pending_fields="$(python3 "$(dirname "$local_script")/vm_backup_resume.py" \
          --check-pending "$vm_pending_receipt")"
        read -r pending_key pending_bytes <<< "$pending_fields"
        if actual_bytes="$(r2 head-object --bucket "$vm_bucket" --key "$pending_key" \
          --query ContentLength --output text 2>/dev/null)" && [[ "$actual_bytes" == "$pending_bytes" ]]; then
          completed_pending=1
          echo 'vm_resume=completed_object_found; proceeding to full audit'
        fi
      fi
      if (( completed_pending == 0 )); then
        python3 "$(dirname "$local_script")/vm_backup_resume.py" \
          "$vm_bucket" "$CLOUDFLARE_R2_ENDPOINT" "$vm_pending_receipt"
      fi
      cp "$vm_pending_receipt" "$scratch/pending-receipt.json"
    else
      [[ -r "$vm_receipt" ]] || { echo 'vm_audit=failed: no receipt' >&2; exit 1; }
      cp "$vm_receipt" "$scratch/pending-receipt.json"
    fi
    python3 - "$scratch/pending-receipt.json" "$CLOUDFLARE_R2_ENDPOINT" <<'PY'
import hashlib, json, subprocess, sys

receipt = json.load(open(sys.argv[1]))
if receipt.get('bucket') != 'virtualos-vm-backups' or not receipt.get('key', '').startswith('vm-backups/'):
    raise SystemExit('vm_audit=failed: invalid receipt destination')
source = subprocess.Popen([
    'aws', 's3', 'cp', f's3://{receipt["bucket"]}/{receipt["key"]}', '-',
    '--endpoint-url', sys.argv[2], '--only-show-errors', '--no-progress'
], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
digest = hashlib.sha256()
size = 0
while chunk := source.stdout.read(1024 * 1024):
    digest.update(chunk)
    size += len(chunk)
    if size and size % (1024 * 1024 * 1024) < len(chunk):
        print(f'vm_audit=downloading compressed_gib={size / 2**30:.1f}', flush=True)
code = source.wait()
if code or size != receipt['bytes'] or digest.hexdigest() != receipt['sha256']:
    raise SystemExit(f'vm_audit=failed code={code} bytes={size} sha256={digest.hexdigest()} '
                     f'error={source.stderr.read().decode(errors="replace")[-1000:]}')
print(f'vm_audit=passed bytes={size} sha256={digest.hexdigest()}')
PY
    if [[ "$mode" == vm-backup || "$mode" == vm-resume ]]; then
      mkdir -p "$(dirname "$vm_receipt")"
      chmod 700 "$(dirname "$vm_receipt")"
      cp "$scratch/pending-receipt.json" "$vm_receipt"
      chmod 600 "$vm_receipt"
      "$sync_script" vm-publish-manifest
      ssh -o ControlMaster=no -o ControlPath=none "$mini_host" "$remote_script vm-clone-drop"
      # The candidate is no longer needed after verified publication and clone
      # release. Leaving it could associate a future clone with an older archive.
      if [[ "$mode" == vm-resume ]]; then
        python3 - "$vm_pending_receipt" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[1]).unlink(missing_ok=True)
PY
      fi
      echo "vm_backup=verified receipt=$vm_receipt"
    fi
    ;;
  vm-probe)
    # A bounded, disposable round trip verifies the storage path without copying
    # a live VM disk or leaving an object behind.
    key="vm-probes/$(date -u +%Y%m%dT%H%M%SZ)-$$.bin"
    trap 'r2 delete-object --bucket "$bucket" --key "$key" >/dev/null 2>&1 || true; rm -rf "$scratch"' EXIT
    dd if=/dev/urandom of="$scratch/probe.bin" bs=1048576 count=1 2>/dev/null
    source_sha="$(shasum -a 256 "$scratch/probe.bin" | cut -d' ' -f1)"
    r2 put-object --bucket "$bucket" --key "$key" --body "$scratch/probe.bin" >/dev/null
    r2 get-object --bucket "$bucket" --key "$key" "$scratch/received.bin" >/dev/null
    received_sha="$(shasum -a 256 "$scratch/received.bin" | cut -d' ' -f1)"
    [[ "$source_sha" == "$received_sha" ]] || {
      echo 'vm_probe=failed: R2 bytes differ' >&2
      exit 1
    }
    echo "vm_probe=passed bytes=1048576 sha256=$source_sha"
    ;;
  status)
    if ! r2 head-bucket --bucket "$bucket" >/dev/null 2>&1; then
      echo "Private bucket $bucket is unavailable" >&2
      exit 1
    fi
    echo "Private R2 bucket $bucket is available"
    ;;
  archive)
    ssh mac-mini-ai "$remote_script receipt" > "$scratch/old-receipt.json" 2>/dev/null || true
    package="$(ssh mac-mini-ai "$remote_script pack")"
    sha="$(printf '%s' "$package" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha256"])')"
    if [[ ! "$sha" =~ ^[0-9a-f]{64}$ ]]; then
      echo 'Invalid package hash from Mini' >&2
      exit 1
    fi
    remote_package="/private/tmp/vos-approved-$sha.tar.gz"
    scp "mac-mini-ai:$remote_package" "$scratch/archive.tar.gz"
    local_sha="$(shasum -a 256 "$scratch/archive.tar.gz" | cut -d' ' -f1)"
    [[ "$local_sha" == "$sha" ]] || { echo 'SCP package checksum failed' >&2; exit 1; }
    audit_archive "$scratch/archive.tar.gz"
    key="approved/$sha.tar.gz"
    r2 put-object --bucket "$bucket" --key "$key" --body "$scratch/archive.tar.gz" --content-type application/gzip >/dev/null
    r2 get-object --bucket "$bucket" --key "$key" "$scratch/verified.tar.gz" >/dev/null
    verified_sha="$(shasum -a 256 "$scratch/verified.tar.gz" | cut -d' ' -f1)"
    [[ "$verified_sha" == "$sha" ]] || { echo 'R2 download checksum failed' >&2; exit 1; }
    audit_archive "$scratch/verified.tar.gz"
    python3 - "$bucket" "$key" "$sha" "$package" > "$scratch/receipt.json" <<'PY'
import json, sys, time
info = json.loads(sys.argv[4])
print(json.dumps({'bucket': sys.argv[1], 'key': sys.argv[2], 'sha256': sys.argv[3],
                  'bytes': info['compressed_bytes'], 'examples': info['examples'],
                  'images': info['images'], 'download_verified': True,
                  'verified_at': time.time()}, sort_keys=True))
PY
    scp "$scratch/receipt.json" mac-mini-ai:/private/tmp/vos-r2-receipt.json
    ssh mac-mini-ai "$remote_script record-receipt"
    ssh mac-mini-ai "$remote_script discard-package"
    old_key="$(python3 - "$scratch/old-receipt.json" "$bucket" <<'PY'
import json, re, sys
try:
    r = json.load(open(sys.argv[1]))
    sha = r.get('sha256', '')
    if (r.get('bucket') == sys.argv[2] and re.fullmatch(r'[0-9a-f]{64}', sha)
            and r.get('key') == 'approved/' + sha + '.tar.gz'):
        print(r['key'])
except (OSError, ValueError):
    pass
PY
)"
    if [[ -n "$old_key" && "$old_key" != "$key" ]]; then
      r2 delete-object --bucket "$bucket" --key "$old_key" >/dev/null
    fi
    echo "Archived $key with $sha verified from R2"
    ;;
  audit|prune)
    ssh mac-mini-ai "$remote_script receipt" > "$scratch/receipt.json"
    read -r key sha < <(python3 - "$scratch/receipt.json" "$bucket" <<'PY'
import json, re, sys
r = json.load(open(sys.argv[1]))
sha = r.get('sha256', '')
if (r.get('bucket') != sys.argv[2] or not re.fullmatch(r'[0-9a-f]{64}', sha)
        or r.get('key') != 'approved/' + sha + '.tar.gz'
        or r.get('download_verified') is not True):
    raise SystemExit('Invalid R2 receipt')
print(r['key'], sha)
PY
)
    r2 get-object --bucket "$bucket" --key "$key" "$scratch/verified.tar.gz" >/dev/null
    verified_sha="$(shasum -a 256 "$scratch/verified.tar.gz" | cut -d' ' -f1)"
    [[ "$verified_sha" == "$sha" ]] || { echo 'R2 checksum failed; local frames retained' >&2; exit 1; }
    audit_archive "$scratch/verified.tar.gz"
    if [[ "$mode" == prune ]]; then
      ssh mac-mini-ai "$remote_script prune"
    fi
    ;;
esac
