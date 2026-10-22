"""Day-long supervision for the two no-argument operator scripts.

Reconnects lost controllers, but never retries a reported integrity failure.
The deadline ends supervision only; existing conversions and VMs keep running.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from cloud_disk_archive_delegate import paths
from cloud_disk_archive_session import safe_file, write_json

TOOLS = Path(__file__).resolve().parent
DURATION = 24 * 3600


def control_tasks():
    """An ordered agenda, not shell commands supplied by mutable state."""
    return [
        dict(id='archive_conversion', title='Verify the completed archive conversion',
             acceptance='Pinned archive, raw disk and manifest hashes match the conversion receipt.'),
        dict(id='r2_readback', dependencies=['archive_conversion'],
             title='Verify the independent full R2 readback',
             acceptance='All 32212254720 raw bytes match the pinned hash; retain the durable audit receipt.'),
        dict(id='recovery_boot', dependencies=['r2_readback'], approval_required=True, resources_ok=False,
             title='Prove the isolated R2 guest boots twice',
             acceptance='Approved test-fleet window, fresh admission, persistent overlay, and independent visual review.'),
        dict(id='ocr_finder', resources_ok=False, title='Prove one private OCR Finder action',
             acceptance='Fresh memory/disk/swap/global-idle admission and independently reviewed before/after frames.'),
        dict(id='controller_improvement', title='Improve these same two shell entrypoints',
             acceptance='Focused controller and integration tests pass; evaluator reviews the exact source hashes.'),
        dict(id='maintenance_readiness', dependencies=['r2_readback'],
             title='Refresh the prepared test-fleet maintenance evidence',
             acceptance='Read-only identity, backup and candidate checks; record a current plan without stopping guests.'),
        dict(id='native_supervisor_activation', dependencies=['recovery_boot'],
             approval_required=True, resources_ok=False,
             title='Activate the tested supervisor during approved maintenance',
             acceptance='Restore vm-a, verify guest input and fresh frames, preserve production and Linux.'),
        dict(id='host_hardening', approval_required=True, resources_ok=False,
             title='Perform approved host hardening',
             acceptance='Owner admin access, SSH rollback, disk budget and verified restart recovery before host changes.'),
    ] + storage_work_tasks()


def storage_work_tasks():
    """Durable storage agenda; these descriptions never become shell commands."""
    rows = [
        ('compressed_mutable_overlay', 'Compress bounded VM write overlays with verified migration',
         'Preserve all disk bytes and existing limits; corruption/restart/FUA tests pass; offline forward/reverse conversions verified; independently reviewed guest boot if activated; production/Linux untouched.'),
        ('storage_baseline_refresh', 'Measure current physical and logical storage',
         'Record allocated versus logical bytes, filesystem free space, open files and time; distinguish shared APFS extents from reclaimable bytes.'),
        ('storage_evidence_refresh', 'Review changed-source storage evidence',
         'Revalidate affected acceptance checks against current source hashes; retain historical deployment hashes and mark unresolved evidence stale.'),
        ('overlay_growth_profile', 'Measure write-overlay growth over several hours',
         'Bounded read-only samples report rows, allocated bytes, WAL bytes and growth rate; no VM restarts or model inference.'),
        ('compressed_overlay_snapshot', 'Round-trip a consistent real overlay snapshot',
         'SQLite backup of the running overlay converts forward and back with identical logical fingerprints; respect 10 GiB reserve and preserve live source.'),
        ('overlay_codec_cost', 'Measure compression CPU and disk tradeoffs',
         'Use closed snapshots or small fixtures; report compression ratio, write/flush latency and bounded memory without guest load generation.'),
        ('overlay_corruption_matrix', 'Exercise malformed compressed writes',
         'Reject truncation, changed checksum, invalid lengths, trailing payload, unknown codec and decompression expansion; failed writes preserve prior bytes.'),
        ('overlay_crash_matrix', 'Exercise interrupted write and conversion recovery',
         'Fixture-only process interruption proves acknowledged FUA writes persist and incomplete conversions never replace the source.'),
        ('overlay_capacity_boundary', 'Verify compressed writes retain logical limits',
         'Compressible input cannot bypass the 2 GiB logical cap; multi-block failures are atomic and base-equivalent writes release rows.'),
        ('overlay_rollback_drill', 'Prove compressed-to-raw rollback offline',
         'Closed snapshot restores using the prior backend with matching disk bytes; document exact helper, config and service restoration order.'),
        ('overlay_service_identity', 'Audit managed disk service identity',
         'Config, seed pin, base identity, codec and running service agree; wrong-format reuse is rejected before a guest attaches.'),
        ('overlay_boot_soak', 'Observe the migrated guest over several hours',
         'After independently verified activation, record fresh relay frames and bounded overlay growth; report gaps and preserve production/Linux PIDs.'),
        ('overlay_wal_audit', 'Audit WAL and checkpoint storage',
         'Measure WAL growth and current checkpoint behavior; identify readers and explain safe checkpoint conditions without editing live SQLite files.'),
        ('overlay_pruning_audit', 'Measure redundant changed-block retention',
         'Closed snapshot audit counts blocks equal to immutable backing and models reclaimable pages without changing live data.'),
        ('immutable_delta_density', 'Measure immutable seed density',
         'Read-only audit reports compressed payload, index/page overhead and raw fallback counts; seed SHA remains unchanged.'),
        ('base_extent_accounting', 'Measure shared-base physical allocation',
         'Record original/base clones and link identities; do not count apparent clone sizes as independent reclaimable storage.'),
        ('original_disk_preservation', 'Verify preserved original VM disk',
         'Original disk exists, remains closed and matches recorded identity; no deletion or replacement of the rollback original.'),
        ('model_reference_refresh', 'Map model manifests to blobs',
         'Each installed manifest and blob is accounted for; shared references and resident models prevent false orphan classification.'),
        ('model_backup_restore_drill', 'Verify the offloaded model backup',
         'Manifest references and every backup blob hash verify from durable operator storage; test metadata restoration in an isolated temporary directory.'),
        ('private_runtime_recheck', 'Recheck compressed private OCR runtime',
         'Pinned runtime byte inventory, signatures and sandboxed help check pass; record actual allocation without running inference.'),
        ('cache_candidate_inventory', 'Find bounded regenerable cache candidates',
         'Read-only inventory identifies owner, regeneration path, allocated size, last use and open-file checks; exclude guest disks and model data.'),
        ('artifact_reference_inventory', 'Find unused build and maintenance artifacts',
         'Trace launch agents, symlinks, imports and rollback references before classifying artifacts; report only evidence-backed candidates.'),
        ('log_growth_inventory', 'Measure logs and retained evidence growth',
         'Report largest growing logs and retention policies; preserve acceptance receipts, private evidence and current service logs.'),
        ('storage_reclaim_plan', 'Prepare exact reviewed reclamation actions',
         'List exact paths, expected physical savings, ownership, references, backup and rollback evidence; no wildcard deletion or automatic approval.'),
        ('r2_archive_reference_audit', 'Audit cloud archive references',
         'Read-only check maps receipts and manifests to archive objects and pending multipart uploads; no cloud deletion.'),
        ('r2_recovery_cost_plan', 'Prepare bounded cloud recovery verification',
         'Estimate download, temporary disk, cache and guest resource needs; retain existing recovery approval/resource gates.'),
        ('host_resource_trend', 'Track disk, memory and swap pressure',
         'Bounded hourly observations identify competing load without stopping other apps or relaxing the existing admission thresholds.'),
        ('storage_alert_thresholds', 'Review storage reserve and growth alerts',
         'Derive warning thresholds from measured growth and existing reserves; report predicted exhaustion without lowering hard safety limits.'),
        ('shell_supervisor_soak', 'Verify hours-long shell supervision',
         'Observe multiple timed ticks, deadline and lock ownership; no duplicate supervisor, unattended worker heartbeat or false completion.'),
        ('shell_reconnect_drill', 'Exercise supervisor outage recovery',
         'Fixture tests retry classified connection failures, preserve completed jobs and refuse integrity-failure retries or gated guest starts.'),
        ('storage_runbook_refresh', 'Document measured storage and recovery procedures',
         'Update existing repository documentation with verified formats, entrypoints, reserves, migration/rollback order and unresolved blockers.'),
        ('storage_final_comparison', 'Compare final storage with the baseline',
         'Report measured physical savings, logical sizes, retained backups and open blockers; staged candidates and apparent clone sizes are not savings.'),
    ]
    dependencies = {
        'overlay_boot_soak': ['compressed_mutable_overlay'],
        'storage_reclaim_plan': ['cache_candidate_inventory', 'artifact_reference_inventory', 'log_growth_inventory'],
        'storage_final_comparison': ['storage_baseline_refresh', 'storage_runbook_refresh'],
    }
    return [dict(id=name, title=title, acceptance=acceptance,
                 dependencies=dependencies.get(name, [])) for name, title, acceptance in rows]


def control_summary(snapshot):
    tasks = snapshot.get('tasks', [])
    active = snapshot.get('leases', {}).get('worker')
    chosen = next((row for row in tasks if active and row['id'] == active['task']), None)
    if chosen is None:
        chosen = next((row for row in tasks if row['role'] == 'worker'
                       and row['status'] == 'pending' and not row['block_reasons']), None)
    return dict(revision=snapshot['revision'],
                completed=[row['id'] for row in tasks if row['status'] == 'done' and row['evidence_valid']],
                next_task=None if chosen is None else {key: chosen[key] for key in
                    ('id', 'title', 'acceptance', 'status')},
                worker=None if active is None else {key: active[key] for key in ('task', 'owner', 'expires_at')},
                blocked={row['id']: row['block_reasons'] for row in tasks if row['block_reasons']},
                stale_leases=len(snapshot.get('stale_leases', [])),
                executes_arbitrary_commands=False)


def control_tick(directory, claim_worker=False):
    from agent_control import Control, ControlError
    from linux_guest_selection import owned_file, unique_object
    repo = TOOLS.parent
    control = Control(directory / 'agent-control', repo)
    control.seed(control_tasks())
    control.reconcile()
    pins = dict(disk_sha256='b8d7a8499d279c74f3f9c9fa13f91fb498f95f14a08c79c403adf4edd3548321',
                manifest_sha256='40f4506a2a21af31d82bca6c900e5d197842d9dd777cc130aa227e29336f6963',
                logical_bytes=32212254720)
    source = dict(account_id='c24a3fb4bedba669785c18c834f51921', bucket='virtualos-cloud-disks',
                  object_prefix='disks/archive-base-0073037110369885')
    imports = [
        ('archive_conversion', 'r2-archive-base-conversion-20261019.json',
         lambda row: row.get('readback_verified') is True and row.get('chunk_count') == 7680
         and row.get('archive_sha256') == '0073037110369885595f21c227c6df3f66f65501daca09844a80c38a07d7b711'
         and row.get('archive_bytes') == 22266108635 and all(row.get(k) == v for k, v in pins.items())
         and isinstance(row.get('config'), dict)
         and all(row.get('config', {}).get(k) == v for k, v in source.items())),
        ('r2_readback', 'r2-independent-base-readback-20261019.json',
         lambda row: row.get('full_r2_readback_verified') is True and row.get('source') == source
         and all(row.get(k) == v for k, v in pins.items())),
        ('controller_improvement', 'agent-control-verification.json',
         lambda row: type(row.get('tests_passed')) is int and row['tests_passed'] > 0
         and row.get('evaluator_review_passed') is True
         and isinstance(row.get('source_sha256'), dict) and 0 < len(row['source_sha256']) <= 128
         and all(type(path) is str and bool(path) and type(pin) is str
                 and len(pin) == 64 and set(pin) <= set('0123456789abcdef')
                 for path, pin in row['source_sha256'].items())),
    ]
    receipt_errors = {}
    for task, filename, validates in imports:
        path = repo / 'tools/experiments' / filename
        if not path.exists():
            continue
        try:
            with owned_file(path, private=False, maximum=65536) as (stream, _):
                body = stream.read(65537)
            if len(body) > 65536:
                raise ValueError('receipt exceeds bound')
            row = json.loads(body, object_pairs_hook=unique_object)
            if not isinstance(row, dict) or not validates(row):
                receipt_errors[task] = 'receipt_contract_mismatch'
                continue
            # Parse and hash the same protected byte snapshot. Attestation
            # rechecks it, so a swapped file cannot certify unvalidated bytes.
            checksum = hashlib.sha256(body).hexdigest()
            current = next(item for item in control.snapshot()['tasks'] if item['id'] == task)
            if current['status'] == 'done' and current['evidence_valid']:
                continue
            if any(reason.startswith('dependency:') or reason in ('approval', 'resources', 'error')
                   for reason in current['block_reasons']):
                continue
            bindings = row.get('source_sha256') if task == 'controller_improvement' else None
            lease = control.snapshot().get('leases', {}).get('worker')
            if current['status'] == 'active':
                if lease is None or lease['task'] != task or lease['owner'] != 'codex-worker':
                    continue
                control.attest(task, str(path), checksum, bindings=bindings)
                control.complete(lease)
            else:
                control.attest(task, str(path), checksum, complete=True, bindings=bindings)
        except (OSError, ValueError, ControlError) as failure:
            receipt_errors[task] = type(failure).__name__
    if claim_worker:
        active = control.snapshot().get('leases', {}).get('worker')
        if active and active['owner'] == 'codex-worker':
            control.heartbeat(active, ttl=300)
        elif active is None:
            control.claim('codex-worker', ttl=300)
    summary = control_summary(control.snapshot())
    summary['receipt_errors'] = receipt_errors
    summary['overlay_growth'] = overlay_growth_observation(directory)
    return summary


def overlay_growth_observation(directory):
    from overlay_growth import read_samples, summarize
    try:
        return summarize(read_samples(directory / 'observer.log'))
    except (OSError, ValueError) as failure:
        return dict(state='unavailable', error=type(failure).__name__)


def control_observation(directory):
    """No queue mutation, leases, heartbeats or commands from the observer."""
    from agent_control import Control
    report = control_summary(Control(directory / 'agent-control', TOOLS.parent).snapshot())
    report['overlay_growth'] = overlay_growth_observation(directory)
    return report


def recovery_admission(directory):
    """Read current gates before dispatch; missing coordination fails closed."""
    from agent_control import Control
    snapshot = Control(directory / 'agent-control', TOOLS.parent).snapshot()
    task = next((row for row in snapshot['tasks'] if row['id'] == 'recovery_boot'), None)
    reasons = ['task_missing'] if task is None else list(task['block_reasons'])
    if task is not None and task['status'] == 'done':
        reasons.append('already_completed')
    worker = snapshot.get('leases', {}).get('worker')
    if worker and worker['task'] != 'recovery_boot':
        reasons.append('worker_busy')
    return dict(allowed=not reasons, task='recovery_boot', block_reasons=reasons)


def background_control():
    # A fresh interpreter reads the updated implementation in the same worker
    # .sh every tick. It only reconciles; it cannot renew an agent's work lease.
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'control-background'],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=15, check=True)
    if len(completed.stdout) > 65536:
        raise ValueError('controller report exceeds bound')
    return json.loads(completed.stdout)


def available(path):
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def read_state(directory, name):
    target = safe_file(directory / name)
    return json.loads(target.read_text()) if target.exists() else {}


def archive_action(state, idle, has_result):
    if has_result or not idle:
        return 'observe'
    if state.get('remote_worker_failed'):
        return 'requires_attention'
    if state.get('state') == 'failed':
        # Older running controllers report exhausted SSH reconnects as local
        # OSError. Mini errors carry remote_worker_failed and were refused above.
        retryable = state.get('retryable_connection_failure') is True or state.get('error') in ('OSError', 'ConnectionError')
        return 'restart' if retryable else 'requires_attention'
    return 'restart'


def step(directory, launch):
    archive = read_state(directory, 'status.json')
    action = archive_action(archive, available(directory / 'lock'), (directory / 'result.json').exists())
    if action == 'restart':
        launch([sys.executable, str(TOOLS / 'cloud_disk_archive_delegate.py'), 'start'])
    linux = read_state(directory, 'linux-work.json')
    if available(directory / 'linux-work.lock') and linux.get('state') not in ('verified', 'requires_attention'):
        launch([sys.executable, str(TOOLS / 'linux_lab_work.py'), 'start'])
    recovery = read_state(directory, 'recovery-watch.json')
    if (action != 'requires_attention' and available(directory / 'recovery-watch.lock')
            and recovery.get('state') not in ('recovery_finished', 'recovery_requires_attention')
            and recovery_admission(directory)['allowed']):
        launch(['bash', str(TOOLS / 'cloud-disk-archive.sh'), 'watch-start'])
    from storage_pruning_job import eligible as pruning_eligible
    if pruning_eligible(directory):
        launch([sys.executable, str(TOOLS / 'storage_pruning_job.py'), 'start'])
    return dict(archive_action=action, archive_state=archive.get('state', 'pending'),
                linux_state=linux.get('state', 'pending'), recovery_state=recovery.get('state', 'pending'))


def main():
    os.umask(0o077)
    directory = paths()
    if sys.argv[1:] == ['recovery-admission']:
        report = recovery_admission(directory)
        print(json.dumps(report, sort_keys=True))
        return 0 if report['allowed'] else 3
    for name in ('work-loop.lock', 'work-loop.json', 'work-loop.json.next', 'work-loop.log',
                 'linux-work.lock', 'recovery-watch.lock'):
        safe_file(directory / name)
    if sys.argv[1:] == ['control']:
        print('agent_control=' + json.dumps(control_tick(directory, claim_worker=True), sort_keys=True))
        return
    if sys.argv[1:] == ['control-background']:
        print(json.dumps(control_tick(directory), sort_keys=True))
        return
    if sys.argv[1:] == ['start']:
        with (directory / 'work-loop.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('work_supervisor=running'); return
            env = {k: v for k, v in os.environ.items() if not (
                k.startswith(('AWS_', 'CF_', 'CLOUDFLARE_')) or
                any(word in k.upper() for word in ('SECRET', 'TOKEN', 'PASSWORD', 'API_KEY')))}
            with (directory / 'work-loop.log').open('a') as log:
                child = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()),
                    'run', str(lock.fileno())], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    env=env, start_new_session=True, pass_fds=(lock.fileno(),))
            print('work_supervisor_started=' + str(child.pid))
        return
    if len(sys.argv) != 3 or sys.argv[1] != 'run':
        raise ValueError('invalid work supervisor operation')
    lock = os.fdopen(int(sys.argv[2]), 'a')
    prior_deadline = read_state(directory, 'work-loop.json').get('deadline')
    now = time.time()
    deadline = prior_deadline if type(prior_deadline) in (int, float) and now < prior_deadline <= now + DURATION else now + DURATION
    def state(**row):
        write_json(directory / 'work-loop.json', dict(pid=os.getpid(), updated_at=time.time(),
                   deadline=deadline, **row))
    def launch(command):
        # The script loads its protected credentials only inside its run mode.
        return subprocess.run(command, stdin=subprocess.DEVNULL, timeout=30, check=True)
    try:
        while time.time() < deadline:
            try:
                row = step(directory, launch)
                row['agent_control'] = background_control()
                state(state='supervising', **row)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                state(state='retrying_supervision', error=type(error).__name__)
            time.sleep(min(60, max(0, deadline - time.time())))
        state(state='supervision_window_finished', existing_jobs_preserved=True)
    finally:
        lock.close()


if __name__ == '__main__':
    sys.exit(main())
