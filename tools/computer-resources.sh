#!/usr/bin/env bash
# Cheap host pressure check before starting background VM or agent work.
set -euo pipefail

if [[ $# -ne 1 || ( "$1" != --report && "$1" != --guard && "$1" != --guard-offline &&
                     "$1" != --stale-simulator-check &&
                     "$1" != --reclaim-stale-simulator ) ]]; then
  echo 'Usage: ./tools/computer-resources.sh --report|--guard|--guard-offline|--stale-simulator-check|--reclaim-stale-simulator' >&2
  exit 2
fi

python3 - "$1" <<'PY'
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict

mode = sys.argv[1]
free_kib = shutil.disk_usage('/System/Volumes/Data').free // 1024
swap = subprocess.run(['sysctl', 'vm.swapusage'], capture_output=True,
                      text=True, timeout=5)
match = re.search(r'used = ([0-9.]+)M', swap.stdout) if swap.returncode == 0 else None
swap_used_mib = round(float(match.group(1))) if match else None
load_one, load_five, load_fifteen = os.getloadavg()
cpu_count = os.cpu_count() or 1
pressure = []
if free_kib < 3 * 1024 * 1024:
    pressure.append('disk_below_3_gib')
if swap_used_mib is None:
    pressure.append('swap_unavailable')
elif swap_used_mib >= 8 * 1024:
    pressure.append('swap_at_least_8_gib')
if load_one >= 2 * cpu_count:
    pressure.append('load_at_least_2x_cpu')
result = {'disk_free_kib': free_kib, 'swap_used_mib': swap_used_mib,
          'load_one': round(load_one, 2), 'cpu_count': cpu_count,
          'background_work': 'paused' if pressure else 'allowed', 'reasons': pressure}
if mode in ('--report', '--guard-offline'):
    try:
        memory = subprocess.check_output(['memory_pressure', '-Q'], text=True,
                                         timeout=5)
        free_match = re.search(r'System-wide memory free percentage:\s*(\d+)%', memory)
        result['memory_free_percent'] = int(free_match.group(1)) if free_match else None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        result['memory_free_percent'] = None
if mode in ('--stale-simulator-check', '--reclaim-stale-simulator'):
    devices = json.loads(subprocess.check_output(
        ['xcrun', 'simctl', 'list', 'devices', '--json'], text=True, timeout=15))
    states = [device.get('state') for group in devices.get('devices', {}).values()
              for device in group]
    if not states or any(state != 'Shutdown' for state in states):
        raise SystemExit('stale_simulator=refused: a simulator is not shutdown')
    processes = subprocess.check_output(
        ['ps', '-axo', 'pid=,ppid=,user=,comm='], text=True, timeout=10)
    rows = [parts for line in processes.splitlines()
            if len(parts := line.strip().split(None, 3)) == 4]
    parents = [row for row in rows if row[2] == 'ai'
               and row[3].rsplit('/', 1)[-1] == 'launchd_sim']
    if len(parents) != 1:
        raise SystemExit('stale_simulator=refused: expected one ai launchd_sim')
    pid = int(parents[0][0])
    children = [row for row in rows if row[1] == str(pid)]
    runtime_children = sum('/CoreSimulator/Volumes/' in row[3] for row in children)
    if (swap_used_mib is None or swap_used_mib < 8 * 1024 or
            len(children) < 50 or runtime_children * 5 < len(children) * 4):
        raise SystemExit('stale_simulator=refused: pressure or child identity check failed')
    report = {'stale_simulator': 'eligible', 'pid': pid,
              'shutdown_devices': len(states), 'children': len(children),
              'runtime_children': runtime_children}
    if mode == '--reclaim-stale-simulator':
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                report['stale_simulator'] = 'terminated'
                break
        else:
            report['stale_simulator'] = 'signal_sent_still_running'
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report['stale_simulator'] in ('eligible', 'terminated') else 1)
if mode == '--report':
    try:
        rows = subprocess.check_output(['ps', '-axo', 'rss=,comm='],
                                       text=True, timeout=10)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        rows = None
    families = {name: [0, 0] for name in
                ('simulator_runtime', 'browser_engines', 'browser_apps',
                 'terminal', 'ai_agents', 'virtualos')}
    executables = defaultdict(lambda: [0, 0])
    for row in (rows or '').splitlines():
        parts = row.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        name = parts[1].lower()
        if '/coresimulator/volumes/' in name:
            family = 'simulator_runtime'
        elif any(term in name for term in ('chrome', 'webkit', 'safari')):
            family = 'browser_engines'
        elif any(term in name for term in ('paper.app', 'pen.app', 'figma.app',
                                          'merge browser', 'openeditor computer')):
            family = 'browser_apps'
        elif any(term in name for term in ('terminal.app', 'iterm.app', 'ghostty.app')):
            family = 'terminal'
        elif any(term in name for term in ('opencode2', 'codex', 'pi-coding-agent',
                                          'ollama')):
            family = 'ai_agents'
        elif 'virtualos' in name or 'virtualization.virtualmachine' in name:
            family = 'virtualos'
        else:
            continue
        families[family][0] += 1
        families[family][1] += int(parts[0])
        executable = parts[1].rsplit('/', 1)[-1].split(' Helper')[0]
        executables[executable][0] += 1
        executables[executable][1] += int(parts[0])
    result['families'] = ({name: {'processes': count, 'sum_rss_mib': round(kib / 1024, 1)}
                           for name, (count, kib) in families.items()} if rows is not None else None)
    result['rss_note'] = 'RSS sums can count shared pages more than once and omit paged-out memory'
    result['top_executables'] = [
        {'name': name, 'processes': count, 'sum_rss_mib': round(kib / 1024, 1)}
        for name, (count, kib) in sorted(executables.items(),
                                         key=lambda item: item[1][1], reverse=True)[:8]
    ]
if mode == '--guard-offline':
    # A single model-free guest action may run while old swap remains allocated,
    # but only with fresh RAM headroom and low load. The general worker keeps
    # the stricter swap gate.
    allowed = (free_kib >= 8 * 1024 * 1024 and
               swap_used_mib is not None and swap_used_mib < 16 * 1024 and
               result['memory_free_percent'] is not None and
               result['memory_free_percent'] >= 25 and load_one < cpu_count)
    result['offline_single_action'] = 'allowed' if allowed else 'paused'
print(json.dumps(result, sort_keys=True))
if mode == '--guard' and pressure:
    raise SystemExit(1)
if mode == '--guard-offline':
    if not allowed:
        raise SystemExit(1)
PY
