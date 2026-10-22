"""Bounded, read-only summaries of the shell observer's overlay measurements."""
import json
import math
import time
from pathlib import Path

BLOCK = 65536
LIMIT = 2 * 1024**3


def read_samples(path, maximum=4 * 1024**2):
    path = Path(path)
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise ValueError('regular observer log required')
    with path.open('rb') as stream:
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(max(0, length - maximum))
        body = stream.read(maximum)
    lines = body.splitlines()
    if length > maximum:
        lines = lines[1:]  # The first line can be a partial JSON record.
    result = []
    for line in lines:
        if line.startswith(b'write_overlay_snapshot={') and len(line) <= 8192:
            try:
                result.append(json.loads(line.split(b'=', 1)[1]))
            except (ValueError, UnicodeError):
                pass
    return result


def summarize(samples, now=None, maximum_gap=120):
    now = time.time() if now is None else now
    accepted = {}
    rejected = 0
    for row in samples:
        if not isinstance(row, dict):
            rejected += 1
            continue
        stamp = row.get('observed_at_unix')
        fields = ('changed_blocks', 'logical_changed_bytes', 'logical_limit_bytes',
                  'allocated_bytes', 'database_bytes', 'wal_bytes')
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or not 0 <= stamp <= now + 5
                or row.get('read_only') is not True or row.get('codec') not in ('raw', 'zlib-v1')
                or any(type(row.get(key)) is not int or not 0 <= row[key] <= 4 * 1024**3
                       for key in fields)
                or row['logical_limit_bytes'] != LIMIT
                or row['logical_changed_bytes'] != row['changed_blocks'] * BLOCK):
            rejected += 1
            continue
        accepted[stamp] = row
    rows = [accepted[key] for key in sorted(accepted)]
    report = dict(state='no_samples', sample_count=len(rows), rejected_samples=rejected,
                  read_only=True, observed_span_seconds=0, gap_count=0,
                  largest_gap_seconds=0, latest_sample_age_seconds=None,
                  recent_contiguous_seconds=0, logical_growth_bytes_per_hour=None,
                  allocated_growth_bytes_per_hour=None,
                  projected_hours_to_logical_limit=None,
                  host_volume={'state': 'unknown'},
                  projection_is_a_guarantee=False)
    if not rows:
        return report
    last = rows[-1]
    gaps = [b['observed_at_unix'] - a['observed_at_unix'] for a, b in zip(rows, rows[1:])]
    begin = len(rows) - 1
    while begin and (rows[begin]['observed_at_unix'] - rows[begin-1]['observed_at_unix'] <= maximum_gap
                     and rows[begin]['codec'] == rows[begin-1]['codec']):
        begin -= 1
    first = rows[begin]
    duration = last['observed_at_unix'] - first['observed_at_unix']
    age = max(0, now - last['observed_at_unix'])
    report.update(state='fresh' if age <= maximum_gap else 'stale',
                  observed_span_seconds=last['observed_at_unix'] - rows[0]['observed_at_unix'],
                  gap_count=sum(gap > maximum_gap for gap in gaps),
                  largest_gap_seconds=max(gaps, default=0), latest_sample_age_seconds=age,
                  recent_contiguous_seconds=duration, latest=last)
    available = last.get('volume_available_bytes')
    if type(available) is int and available >= 0:
        report['host_volume'] = dict(
            state='fresh' if age <= maximum_gap else 'stale', available_bytes=available,
            host_reserve_reference_bytes=10 * 1024**3,
            host_reserve_reference_met=(available >= 10 * 1024**3) if age <= maximum_gap else None,
            vm_start_disk_reference_bytes=14 * 1024**3,
            vm_start_disk_reference_met=(available >= 14 * 1024**3) if age <= maximum_gap else None,
            bytes_short_of_vm_start_disk_reference=max(0, 14 * 1024**3 - available),
            reference_thresholds_do_not_replace_live_resource_admission=True)
    # A gap or codec change starts a new segment. Never infer a rate through an outage.
    if age <= maximum_gap and duration >= 60:
        for field, name in [('logical_changed_bytes', 'logical_growth_bytes_per_hour'),
                            ('allocated_bytes', 'allocated_growth_bytes_per_hour')]:
            report[name] = (last[field] - first[field]) * 3600 / duration
        rate = report['logical_growth_bytes_per_hour']
        if duration >= 300 and rate > 0:
            report['projected_hours_to_logical_limit'] = max(0, LIMIT - last['logical_changed_bytes']) / rate
    return report
