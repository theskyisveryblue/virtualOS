#!/usr/bin/env bash
# One operator entry point for bounded private computer-use work.
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^(--continuous|--continuous-restart|--status|--once|--dock-practice|--research-smoke|--research-swarm|--research-status)$ ]]; then
  echo 'Usage: ./tools/computer-work.sh --continuous|--continuous-restart|--status|--once|--dock-practice|--research-smoke|--research-swarm|--research-status' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
mode="$1"
if [[ "$mode" == --status ]]; then
  worker_pid="$(pgrep -f '[c]omputer-work.sh --continuous$' 2>/dev/null | head -1 || true)"
  screen_list="$(screen -ls 2>&1 || true)"
  if grep -q '[.]virtualos-work' <<< "$screen_list"; then
    if [[ "$screen_list" =~ ([0-9]+)[.]virtualos-work ]]; then
      session_pid="${BASH_REMATCH[1]}"
    else
      session_pid='unknown'
    fi
    elapsed="$(ps -o etime= -p "$session_pid" 2>/dev/null | tr -d ' ' || true)"
    printf 'work=running_detached session_pid=%s worker_pid=%s elapsed=%s\n' \
      "$session_pid" "${worker_pid:-unknown}" "${elapsed:-unknown}"
  elif [[ -n "$worker_pid" ]]; then
    elapsed="$(ps -o etime= -p "$worker_pid" 2>/dev/null | tr -d ' ' || true)"
    printf 'work=running pid=%s elapsed=%s\n' "$worker_pid" "${elapsed:-unknown}"
  else
    echo 'work=stopped'
  fi
  log="$HOME/Library/Logs/virtualos-computer-work.log"
  [[ -f "$log" ]] && tail -n 8 "$log"
  exit 0
fi
if [[ "$mode" == --continuous-restart ]]; then
  command -v screen >/dev/null || { echo 'work=refused: screen is unavailable' >&2; exit 1; }
  log="$HOME/Library/Logs/virtualos-computer-work.log"
  mkdir -p "$(dirname "$log")"
  touch "$log"
  chmod 600 "$log"
  screen -S virtualos-work -X quit >/dev/null 2>&1 || true
  # `screen -X quit` may leave its shell child running after the socket exits.
  # Stop only this exact continuous worker before starting the replacement.
  while read -r old_pid; do
    [[ "$old_pid" =~ ^[0-9]+$ ]] || continue
    kill "$old_pid" 2>/dev/null || true
  done < <(pgrep -f 'computer-work[.]sh --continuous$' || true)
  for (( attempt=0; attempt<10; attempt++ )); do
    if ! pgrep -f 'computer-work[.]sh --continuous$' >/dev/null 2>&1; then break; fi
    sleep 1
  done
  if pgrep -f 'computer-work[.]sh --continuous$' >/dev/null 2>&1; then
    echo 'work=restart_refused: previous continuous worker remains' >&2
    exit 1
  fi
  screen -dmS virtualos-work bash -c '"$1" --continuous >> "$2" 2>&1' _ "$repo_dir/tools/computer-work.sh" "$log"
  running=0
  for (( attempt=0; attempt<30; attempt++ )); do
    if screen -ls 2>/dev/null | grep '[.]virtualos-work' >/dev/null ||
       pgrep -f '[c]omputer-work.sh --continuous' >/dev/null 2>&1; then
      running=1
      break
    fi
    sleep 1
  done
  if (( ! running )); then
    echo "work=restart_failed log=$log" >&2
    exit 1
  fi
  printf 'work=running_detached hours=%s log=%s\n' "${VOS_CUA_WORK_HOURS:-10}" "$log"
  exit 0
fi
if [[ "$mode" == --research-status ]]; then
  python3 - <<'PY'
import json, pathlib
root = pathlib.Path('/private/tmp/virtualos-research')
for path in sorted(root.glob('*.status.json')):
    try:
        item = json.loads(path.read_text())
        print(f"{path.stem}: {item.get('state')} seconds={item.get('seconds')} bytes={item.get('bytes')}")
    except (ValueError, OSError):
        print(f'{path.stem}: unreadable status')
PY
  exit 0
fi
if [[ "$mode" == --research-smoke || "$mode" == --research-swarm ]]; then
  "$repo_dir/tools/computer-resources.sh" --guard || {
    echo 'research=paused: host resource pressure' >&2
    exit 1
  }
  command -v opencode2 >/dev/null || { echo 'research=refused: opencode2 is unavailable' >&2; exit 1; }
  umask 077
  python3 - "$mode" <<'PY'
import concurrent.futures, json, os, pathlib, subprocess, sys, tempfile, time

mode = sys.argv[1]
root = pathlib.Path('/private/tmp/virtualos-research')
root.mkdir(mode=0o700, exist_ok=True)
os.chmod(root, 0o700)
topics = '''
Virtualization disk attachment formats|Does the installed Apple SDK support ASIF VM boot, and through which initializer?
Virtualization NBD API|What exact macOS and entitlement requirements apply to Apple's network block attachment?
macOS guest disk resize|Which supported APIs can enlarge a macOS guest disk and its APFS container?
APFS sparse reclamation|How can a sparse RAW VM file release host blocks without damaging APFS guest data?
Apple Silicon compression|Which public hardware compression paths can accelerate a VM backup stream?
Consistent live VM backup|What guest pause or APFS clone sequence produces a consistent VM backup?
APFS snapshot semantics|What exactly does an APFS clone preserve when source blocks change during upload?
Sparse RAW roundtrip|How should a RAW disk archive preserve holes and verify every logical byte?
ASIF conversion|What space and boot support are required for a safe RAW to ASIF VM migration?
Restore verification|How can a VM backup be boot tested without altering production identity or disk?
R2 multipart limits|What are current R2 multipart part sizes, limits, and abort behavior?
R2 operation billing|How are multipart writes and reads counted against R2 Standard free operations?
R2 storage billing|How is retained and pending multipart storage billed over time?
R2 lifecycle cleanup|Can lifecycle rules clean abandoned multipart uploads safely?
R2 object integrity|Which checksums are verified on multipart upload, download, and range reads?
NBD over R2|What NBD protocol operations require a durable local overlay over immutable R2 chunks?
HTTP ranges and cache|What chunk size minimizes R2 request count without hurting cold boot latency?
FUSE semantics|Which R2 FUSE semantics make it unsafe for a live writable VM disk?
SSD writeback cache|What flush, FUA, and crash ordering guarantees must an NBD overlay provide?
Offline boot|How large and how selected must a pinned boot cache be to survive R2 outage?
Guest screenshot API|Which public Apple APIs offer lowest latency capture of a VZ guest view?
Guest input API|Which public APIs safely deliver keyboard and pointer input to a macOS VZ guest?
App identity oracle|Can a host independently verify the frontmost app inside a macOS guest?
OCR title verification|What OCR evidence is sufficient to verify a guest app launch or menu action?
Keyboard reliability|How do keyboard layouts and clipboard behavior affect guest text input?
gzip versus zstd|What is the measured M-series throughput and ratio tradeoff for sparse RAW backups?
Zero block scanning|Can SIMD or sparse-file APIs skip zero blocks without losing disk correctness?
Disk conversion formats|Would qcow2 or VHDX provide a supported Virtualization.framework attachment?
APFS compression|Can APFS file compression help a RAW VM disk without breaking its writes?
Upload bottleneck|Which measurements distinguish disk read, compressor, network, and R2 upload limits?
Intent schema|What minimal goal schema captures task intent, app, expected state, and privacy boundary?
Outcome oracle|How should computer-use traces label actual success independently of the actor?
Abstention policy|When should a vision agent abstain or request a new frame instead of guessing?
Task state|How can an agent track progress and failure across a multi-step app task?
App action vocabulary|Which app-specific action metadata improves transfer without capturing private content?
Screenshot minimization|How can training traces retain task signal while minimizing retained pixels?
On-device redaction|What local redaction methods can remove personal text before any upload?
Per-app metadata|Which per-app fields are useful for training yet avoid sensitive content?
Private encryption|Does client-side encryption materially improve R2 backup privacy and restore safety?
Review provenance|What review record proves which examples were approved for training?
Trajectory scoring|Which metrics assess computer-use plans beyond individual click correctness?
Counterfactual tests|How can action alternatives be evaluated safely without touching production guest state?
App launch oracle|What observable signals distinguish a successful app launch from a changed screenshot?
Failure taxonomy|How should repeated blank frames, bad coordinates, and stale state be classified?
Style validity|Which user style patterns are stable and task-relevant rather than private content?
Cloudflare Containers|Can Cloudflare Containers host an Apple silicon macOS VM under current limits?
Browser stream|Which transport safely streams an Apple VM desktop to a browser through Cloudflare Access?
Remote latency|What encode and network latency budget makes remote computer use responsive?
R2 block economics|What read/write counts and cache hit rate keep a remote block store affordable?
Hybrid architecture|What staged architecture moves cold VM blocks to R2 while preserving local boot and restore?
'''.strip().splitlines()
assert len(topics) == 50
config = {'agents': {'cua_research': {'mode': 'primary', 'steps': 6,
    'system': 'Read-only hypothesis generation. Do not call tools. Treat all claims as unverified until a separate source check. Never access local files, credentials, account data, or private material. Never execute code or write files.',
    'permissions': [{'action': '*', 'resource': '*', 'effect': 'deny'}]}}}
env = os.environ.copy()
env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config, separators=(',', ':'))
model = 'huggingface/deepseek-ai/DeepSeek-V4.1-Flash'

def run(index, item):
    title, question = item.split('|', 1)
    prefix = root / f'{index:02d}'
    result = prefix.with_suffix('.jsonl')
    status = prefix.with_suffix('.status.json')
    if status.exists() and mode != '--research-smoke':
        try:
            prior = json.loads(status.read_text())
            if prior.get('state') == 'complete':
                return index, 'already_complete'
        except ValueError:
            pass
    free_bytes = os.statvfs(root).f_bavail * os.statvfs(root).f_frsize
    if free_bytes < 768 * 1024**2:
        status.write_text(json.dumps({'title': title, 'state': 'low_disk',
                                      'free_bytes': free_bytes}) + '\n')
        return index, 'low_disk'
    prompt = f'{question} Do not call tools. This is hypothesis generation: label every claim unverified. Give official source URLs to check later, a concrete experiment, expected effect, uncertainty, and a stop condition. Keep the answer under 300 words. Do not inspect local files or accounts.'
    cmd = ['opencode2', 'run', '--standalone', '--agent', 'cua_research', '--model', model,
           '--format', 'json', '--title', f'virtualos-research-{index:02d}', prompt]
    start = time.monotonic()
    with tempfile.TemporaryFile(dir=root) as raw:
        try:
            child = subprocess.Popen(cmd, env=env, cwd='/private/tmp', stdout=raw,
                                     stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 240
            code = None
            while child.poll() is None:
                if time.monotonic() >= deadline:
                    code = 124
                    child.kill()
                    break
                if os.fstat(raw.fileno()).st_size > 2 * 1024**2:
                    code = 125
                    child.kill()
                    break
                time.sleep(0.25)
            child.wait(timeout=10)
            if code is None:
                code = child.returncode
        except OSError:
            code = 127
        raw.seek(0, 2)
        raw_bytes = raw.tell()
        raw.seek(0)
        texts, sources, tool_errors = [], set(), []
        for line in raw:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            part = event.get('part', {})
            if event.get('type') == 'text':
                texts.append(part.get('text', ''))
            if event.get('type') == 'tool_use':
                state_info = part.get('state', {})
                if state_info.get('status') == 'error':
                    tool_errors.append(str(state_info.get('error', ''))[:100])
                link = state_info.get('input', {}).get('url')
                if isinstance(link, str) and link.startswith('https://'):
                    sources.add(link)
    answer = ''.join(texts)[-12000:]
    state = ('timeout' if code == 124 else
             'output_limit' if code == 125 else
             'complete' if code == 0 and len(answer.strip()) >= 100 else 'failed')
    result_next = result.with_name(result.name + f'.next.{os.getpid()}')
    result_next.write_text(json.dumps({'title': title, 'question': question,
                                       'answer': answer, 'source_urls': sorted(sources)[:20],
                                       'tool_errors': tool_errors[:5]}) + '\n')
    result_next.replace(result)
    size = result.stat().st_size
    status_next = status.with_name(status.name + f'.next.{os.getpid()}')
    status_next.write_text(json.dumps({'title': title, 'state': state, 'exit_code': code,
                                       'seconds': round(time.monotonic()-start, 1),
                                       'bytes': size, 'raw_bytes_discarded': raw_bytes}) + '\n')
    status_next.replace(status)
    return index, state

items = topics[:1] if mode == '--research-smoke' else topics
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    queue = iter(enumerate(items, 1))
    running = {}
    for pair in (next(queue, None), next(queue, None)):
        if pair is not None:
            running[pool.submit(run, *pair)] = pair[0]
    failures = 0
    while running:
        done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
        for future in done:
            running.pop(future)
            index, state = future.result()
            print(f'research_job={index:02d} state={state}', flush=True)
            if state == 'low_disk':
                print('research=paused: operator disk below 768 MiB free', flush=True)
                sys.exit(1)
            if mode == '--research-smoke' and state not in ('complete', 'already_complete'):
                sys.exit(1)
            failures = failures + 1 if state in ('failed', 'timeout', 'output_limit') else 0
            if failures >= 3:
                print('research=paused: three consecutive provider failures', flush=True)
                sys.exit(1)
            pair = next(queue, None)
            if pair is not None:
                running[pool.submit(run, *pair)] = pair[0]
PY
  exit $?
fi
if [[ "$mode" == --continuous || "$mode" == --once ]]; then
  command -v ssh >/dev/null || { echo 'work=refused: ssh is unavailable' >&2; exit 1; }
  command -v aws >/dev/null || { echo 'work=refused: AWS CLI is unavailable' >&2; exit 1; }
  hours="${VOS_CUA_WORK_HOURS:-10}"
  interval="${VOS_CUA_WORK_INTERVAL:-300}"
  [[ "$hours" =~ ^[1-9][0-9]*$ && "$interval" =~ ^[1-9][0-9]*$ ]] || {
    echo 'work=refused: invalid hours or interval' >&2
    exit 2
  }
  end_at="$(( $(date +%s) + hours * 3600 ))"
  stop="$HOME/.config/virtualos/computer-work-stop"
  last_audit=0
  last_verified_at=0
  last_audited_sha=''
  last_dataset_rows=''
  last_offline_attempt="$(date +%s)"
  echo "work=started mode=$mode hours=$hours interval_seconds=$interval"
  while :; do
    now="$(date +%s)"
    if [[ -e "$stop" ]]; then
      echo 'work=stopped: stop marker present'
      exit 0
    fi
    if [[ "$mode" == --continuous && "$now" -ge "$end_at" ]]; then
      echo 'work=finished: bounded window elapsed'
      exit 0
    fi
    cycle_failed=0
    receipt_sha=''
    if status="$(ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
      '/Users/ai/vos-fleet/tools/computer-status.sh' 2>/dev/null)"; then
      disk_kb="$(printf '%s\n' "$status" | awk -F= '$1=="disk_free_kib" {print $2; exit}')"
      runner_state="$(printf '%s\n' "$status" | awk -F'[ =]' '$1=="quality_runner" {print $2; exit}')"
      receipt_sha="$(printf '%s\n' "$status" | awk -F= '$1=="r2_receipt_sha256" {print $2; exit}')"
      printf 'work=mini_connected disk_free_kib=%s quality_runner=%s\n' \
        "${disk_kb:-unknown}" "${runner_state:-unknown}"
      if [[ "$runner_state" == stopped ]]; then
        remaining_hours="$(( (end_at - now + 3599) / 3600 ))"
        if (( remaining_hours > 0 )); then
          if ! ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
            '/Users/ai/vos-fleet/tools/computer-resources.sh --guard' >/dev/null 2>&1; then
            echo 'work=mini_observation_paused_by_resource_guard'
            if (( now - last_offline_attempt >= 3600 )) &&
               ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
                 '/Users/ai/vos-fleet/tools/computer-resources.sh --guard-offline' >/dev/null 2>&1; then
              last_offline_attempt="$now"
              if target_json="$(ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
                '/Users/ai/vos-fleet/tools/computer-decision.sh --next-visible-finder-target')"; then
                target="$(python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("target") or "")
except (ValueError, TypeError):
    print("")' <<< "$target_json")"
              else
                target=''
                echo 'work=offline_curriculum_probe_unavailable'
              fi
              if [[ "$target" =~ ^(Applications|Downloads|Documents|Desktop|Recents)$ ]]; then
                if ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
                  "cd /Users/ai/vos-fleet && ./tools/computer-journey.sh --offline-only --one-shot --target $target"; then
                  echo "work=offline_curriculum_verified target=$target"
                else
                  echo "work=offline_curriculum_deferred target=$target"
                fi
              else
                echo 'work=offline_curriculum_no_visible_target'
              fi
            fi
          elif ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
            "cd /Users/ai/vos-fleet && VOS_CUA_OVERNIGHT_HOURS=$remaining_hours VOS_CUA_QUALITY_CURRICULUM=1 ./tools/computer-start.sh --quality"; then
            echo 'work=mini_observation_started'
          else
            echo 'work=mini_observation_start_failed'
            cycle_failed=1
          fi
        fi
      fi
    else
      echo 'work=mini_unavailable; retrying later'
      cycle_failed=1
    fi
    if (( now - last_audit >= 3600 )); then
      for trace_dir in \
        '/Users/ai/Library/Application Support/virtualOS/computer-use' \
        '/Users/ai/Library/Application Support/virtualOS/computer-use/journeys'; do
        if ! ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
          "/Users/ai/vos-fleet/tools/computer-decision.sh --ingest-traces '$trace_dir'"; then
          echo 'work=offline_trace_ingest_deferred'
        fi
      done
      if policy_stats="$(ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
        '/Users/ai/vos-fleet/tools/computer-decision.sh --stats' 2>/dev/null)"; then
        feedback_rows="$(printf '%s' "$policy_stats" | python3 -c \
          'import json,sys; d=json.load(sys.stdin); print(str(d.get("feedback_rows", ""))+":"+str(d.get("source_app_rows", "")))')" || feedback_rows=''
        if [[ "$feedback_rows" =~ ^[0-9]+:[0-9]+$ && "$feedback_rows" != "$last_dataset_rows" ]]; then
          if ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
            '/Users/ai/vos-fleet/tools/computer-decision.sh --export-local'; then
            last_dataset_rows="$feedback_rows"
            echo "work=offline_dataset_updated feedback_source_counts=$feedback_rows"
            if ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
              '/Users/ai/vos-fleet/tools/computer-decision.sh --train-local'; then
              echo 'work=offline_linear_updated'
            else
              echo 'work=offline_linear_deferred'
            fi
          else
            echo 'work=offline_dataset_update_failed'
          fi
        fi
      else
        echo 'work=offline_dataset_status_unavailable'
      fi
      if ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
        '/Users/ai/vos-fleet/tools/computer-model-api.sh --self-test-mini'; then
        echo 'work=model_api_passed'
      elif ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
        '/Users/ai/vos-fleet/tools/computer-resources.sh --guard' >/dev/null 2>&1 &&
           ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
        '/Users/ai/vos-fleet/tools/computer-model-api.sh --start-mini' &&
           ssh -o BatchMode=yes -o ConnectTimeout=8 mac-mini-ai \
        '/Users/ai/vos-fleet/tools/computer-model-api.sh --self-test-mini'; then
        echo 'work=model_api_recovered'
      else
        echo 'work=model_api_deferred'
        cycle_failed=1
      fi
      local_free_kb="$(df -Pk /private/tmp | awk 'NR==2 {print $4}')"
      if [[ "$local_free_kb" =~ ^[0-9]+$ ]] && (( local_free_kb >= 524288 )); then
        if "$repo_dir/tools/computer-space.sh" --ax-codec-measure >/dev/null &&
           "$repo_dir/tools/computer-dataset.sh" --playbook-self-test &&
           "$repo_dir/tools/computer-check.sh" --retention-self-test &&
           "$repo_dir/tools/computer-decision.sh" --self-test; then
          echo 'work=private_format_checks_passed'
        else
          echo 'work=private_format_checks_failed'
          cycle_failed=1
        fi
      else
        echo "work=private_format_checks_skipped_low_disk free_kib=${local_free_kb:-unknown}"
      fi
      if [[ ! "$receipt_sha" =~ ^[0-9a-f]{64}$ ]]; then
        echo 'work=r2_audit_skipped_missing_receipt'
        cycle_failed=1
      elif [[ "$receipt_sha" == "$last_audited_sha" ]] && (( now - last_verified_at < 21600 )); then
        echo 'work=r2_audit_skipped_unchanged_receipt'
      elif [[ ! "$local_free_kb" =~ ^[0-9]+$ || "$local_free_kb" -lt 1048576 ]]; then
        echo "work=r2_audit_skipped_low_local_disk free_kib=${local_free_kb:-unknown}"
        cycle_failed=1
      elif "$repo_dir/tools/computer-r2-sync.sh" audit; then
        echo 'work=r2_audit_passed'
        last_audited_sha="$receipt_sha"
        last_verified_at="$now"
      else
        echo 'work=r2_audit_failed; no archive was changed'
        cycle_failed=1
      fi
      last_audit="$now"
    fi
    if [[ "$mode" == --once ]]; then
      echo 'work=one_cycle_complete'
      exit "$cycle_failed"
    fi
    sleep "$interval"
  done
fi

# Repetitive Dock practice is retained only as an explicit diagnostic.
export VOS_CUA_MIN_FREE_KB="${VOS_CUA_MIN_FREE_KB:-1048576}"
tasks=(
  'Click the black Terminal icon in the guest Dock'
  'Click the blue Safari compass icon in the guest Dock'
  'Click the multicolor Google Chrome icon in the guest Dock'
  'Click the blue smiling Finder icon in the guest Dock'
)

completed=0
for task in "${tasks[@]}"; do
  "$repo_dir/tools/computer-wake.sh"
  "$repo_dir/tools/computer-unlock.sh"
  echo "task=$task"
  result="$("$repo_dir/tools/computer-verify.sh" --act "$task")"
  printf '%s\n' "$result"
  if printf '%s\n' "$result" | python3 -c 'import json,sys; rows=[json.loads(x) for x in sys.stdin if x.startswith("{")]; sys.exit(0 if rows and rows[-1].get("status")=="complete" and rows[-1].get("verified") is True else 1)'; then
    completed=$((completed + 1))
  else
    echo 'task_result=unverified; stopping work' >&2
    exit 1
  fi
done
printf 'verified_tasks=%s\n' "$completed"
"$repo_dir/tools/computer-status.sh"
