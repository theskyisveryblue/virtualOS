#!/usr/bin/env bash
# Package reviewed guest traces on the Mini; retain failures during later pruning.
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^(pack|receipt|record-receipt|discard-package|prune|vm-clone|vm-clone-status|vm-clone-drop|vm-compression-probe|vm-asif-probe|vm-asif-sample|vm-asif-stream-probe)$ ]]; then
  echo 'Usage: ./tools/computer-r2.sh pack|receipt|record-receipt|discard-package|prune|vm-clone|vm-clone-status|vm-clone-drop|vm-compression-probe|vm-asif-probe|vm-asif-sample|vm-asif-stream-probe' >&2
  exit 2
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
mode="$1"
root="${VOS_CUA_TRACE_DIR:-$HOME/Library/Application Support/virtualOS/computer-use}"
vm_bundle="$HOME/VirtualOSMachines/virtualOS.bundle"
vm_clone="$HOME/VirtualOSMachines/.virtualOS-r2-staging.bundle"
vm_agent="$HOME/Library/LaunchAgents/com.github.yep.virtualos.start.plist"
vm_domain="gui/$(id -u)"

if [[ "$mode" == vm-asif-stream-probe ]]; then
  temp="$(mktemp -d /private/tmp/vos-asif-stream-probe.XXXXXX)"
  device=''
  clean_probe() {
    if [[ -n "$device" ]]; then diskutil eject "$device" >/dev/null 2>&1 || true; fi
    rm -rf "$temp"
  }
  trap clean_probe EXIT
  diskutil image create blank --format RAW --size 128m --fs APFS "$temp/Raw.img" >/dev/null
  diskutil image create blank --format ASIF --size 128m --fs None "$temp/Disk.asif" >/dev/null
  device="$(diskutil image attach --noMount "$temp/Disk.asif" | awk 'NR==1 {print $1}')"
  [[ "$device" =~ ^/dev/disk[0-9]+$ ]] || { echo "vm_asif_stream_probe=failed: invalid device $device" >&2; exit 1; }
  dd if="$temp/Raw.img" of="$device" bs=1m 2>/dev/null
  diskutil eject "$device" >/dev/null
  device=''
  diskutil image create from --format RAW "$temp/Disk.asif" "$temp/Roundtrip.img" >/dev/null
  cmp "$temp/Raw.img" "$temp/Roundtrip.img"
  full_alloc="$(du -sk "$temp/Disk.asif" | awk '{print $1}')"
  diskutil image create blank --format ASIF --size 128m --fs None "$temp/Sparse.asif" >/dev/null
  device="$(diskutil image attach --noMount "$temp/Sparse.asif" | awk 'NR==1 {print $1}')"
  [[ "$device" =~ ^/dev/disk[0-9]+$ ]] || { echo "vm_asif_stream_probe=failed: invalid sparse device $device" >&2; exit 1; }
  python3 - "$temp/Raw.img" "$device" <<'PY'
import os, sys
zero = bytes(4096)
with open(sys.argv[1], 'rb', buffering=0) as raw, open(sys.argv[2], 'r+b', buffering=0) as image:
    offset = 0
    while chunk := raw.read(4096):
        if chunk != zero:
            written = os.pwrite(image.fileno(), chunk, offset)
            if written != len(chunk):
                raise OSError('short ASIF block write')
        offset += len(chunk)
PY
  diskutil eject "$device" >/dev/null
  device=''
  diskutil image create from --format RAW "$temp/Sparse.asif" "$temp/SparseRoundtrip.img" >/dev/null
  cmp "$temp/Raw.img" "$temp/SparseRoundtrip.img"
  printf 'vm_asif_stream_probe=passed raw_alloc_kib=%s full_write_asif_alloc_kib=%s sparse_write_asif_alloc_kib=%s\n' \
    "$(du -sk "$temp/Raw.img" | awk '{print $1}')" \
    "$full_alloc" "$(du -sk "$temp/Sparse.asif" | awk '{print $1}')"
  exit 0
fi

if [[ "$mode" == vm-asif-sample ]]; then
  [[ -f "$vm_clone/.complete" ]] || { echo 'vm_asif_sample=refused: no complete clone' >&2; exit 1; }
  temp="$(mktemp -d /private/tmp/vos-asif-sample.XXXXXX)"
  trap 'rm -rf "$temp"' EXIT
  python3 - "$vm_clone/Disk.img" "$temp/Sample.img" <<'PY'
import os, sys
source, target = sys.argv[1:]
size = os.path.getsize(source)
block = 1024 * 1024
with open(source, 'rb', buffering=0) as disk, open(target, 'wb') as sample:
    for index in range(64):
        disk.seek((size - block) * index // 63)
        sample.write(disk.read(block))
PY
  raw_alloc="$(du -sk "$temp/Sample.img" | awk '{print $1}')"
  if diskutil image create from --format ASIF "$temp/Sample.img" "$temp/Sample.asif" >"$temp/result.log" 2>&1; then
    asif_alloc="$(du -sk "$temp/Sample.asif" | awk '{print $1}')"
    printf 'vm_asif_sample=converted raw_alloc_kib=%s asif_alloc_kib=%s\n' "$raw_alloc" "$asif_alloc"
  else
    printf 'vm_asif_sample=failed error=%s\n' "$(tail -1 "$temp/result.log")"
  fi
  exit 0
fi

if [[ "$mode" == vm-asif-probe ]]; then
  temp="$(mktemp -d /private/tmp/vos-asif-probe.XXXXXX)"
  trap 'rm -rf "$temp"' EXIT
  diskutil image create blank --format RAW --size 128m --fs APFS "$temp/Raw.img" >/dev/null
  before="$(stat -f %z "$temp/Raw.img")"
  if diskutil image create from --format ASIF "$temp/Raw.img" "$temp/Raw.img" >"$temp/result.log" 2>&1; then
    printf 'vm_asif_probe=in_place_success before=%s after=%s\n' "$before" "$(stat -f %z "$temp/Raw.img")"
  else
    printf 'vm_asif_probe=in_place_failed before=%s error=%s\n' "$before" "$(tail -1 "$temp/result.log")"
  fi
  exit 0
fi

if [[ "$mode" == vm-compression-probe ]]; then
  [[ -f "$vm_clone/.complete" ]] || { echo 'vm_compression_probe=refused: no complete clone' >&2; exit 1; }
  python3 - "$vm_clone/Disk.img" <<'PY'
import json, lzma, os, sys, time, zlib

path = sys.argv[1]
size = os.path.getsize(path)
block = 1024 * 1024
count = 64
samples = bytearray()
with open(path, 'rb', buffering=0) as stream:
    for index in range(count):
        stream.seek((size - block) * index // (count - 1))
        samples.extend(stream.read(block))
raw = bytes(samples)
measurements = {}
for name, encode in (('gzip_level_1_equivalent', lambda: zlib.compress(raw, 1)),
                     ('lzma_preset_1', lambda: lzma.compress(raw, preset=1))):
    started = time.monotonic()
    blob = encode()
    measurements[name] = {'ratio': round(len(blob) / len(raw), 4),
                          'seconds': round(time.monotonic() - started, 2)}
print(json.dumps({'sample_bytes': len(raw), 'disk_logical_bytes': size,
                  'methods': measurements}, sort_keys=True))
PY
  exit 0
fi

if [[ "$mode" == vm-clone-status ]]; then
  if [[ -d "$vm_clone" ]]; then
    [[ -f "$vm_clone/.complete" ]] && echo 'clone=complete' || echo 'clone=incomplete'
    stat -f 'clone_created=%Sm' -t '%Y-%m-%dT%H:%M:%S%z' "$vm_clone"
    du -sh "$vm_clone"
    stat -f 'disk_logical_bytes=%z' "$vm_clone/Disk.img"
  else
    echo 'clone=absent'
  fi
  df -h /System/Volumes/Data | tail -1
  exit 0
fi

if [[ "$mode" == vm-clone ]]; then
  free_kib="$(df -Pk /System/Volumes/Data | awk 'NR==2 {print $4}')"
  (( free_kib >= 10485760 )) || { echo 'vm_clone=refused: less than 10 GiB free' >&2; exit 1; }
  if [[ -d "$vm_clone" && ! -L "$vm_clone" ]]; then
    for name in Disk.img AuxiliaryStorage HardwareModel MachineIdentifier Parameters.txt; do
      [[ -f "$vm_clone/$name" ]] || { echo "vm_clone=refused: incomplete $name" >&2; exit 1; }
      [[ "$(stat -f %z "$vm_bundle/$name")" == "$(stat -f %z "$vm_clone/$name")" ]] || {
        echo "vm_clone=refused: size mismatch $name" >&2; exit 1;
      }
    done
    if lsof "$vm_clone/Disk.img" >/dev/null 2>&1; then
      echo 'vm_clone=refused: clone disk is open' >&2; exit 1
    fi
    touch "$vm_clone/.complete"
    chmod 600 "$vm_clone/.complete"
    echo "vm_clone=reused path=$vm_clone"
    exit 0
  fi
  [[ ! -e "$vm_clone" && -f "$vm_bundle/Disk.img" && -f "$vm_agent" ]] || {
    echo 'vm_clone=refused: source, launch agent, or unused destination check failed' >&2
    exit 1
  }
  agent_was_loaded=0
  if launchctl list com.github.yep.virtualos.start >/dev/null 2>&1; then
    agent_was_loaded=1
    launchctl bootout "$vm_domain" "$vm_agent"
  fi
  restore_vm() {
    if (( agent_was_loaded )); then
      launchctl bootstrap "$vm_domain" "$vm_agent" || true
      launchctl kickstart "$vm_domain/com.github.yep.virtualos.start" || true
    else
      "$repo_dir/tools/vosctl" start || true
    fi
  }
  trap restore_vm EXIT
  "$repo_dir/tools/vosctl" stop
  for (( attempt=0; attempt<60; attempt++ )); do
    if ! lsof "$vm_bundle/Disk.img" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  if lsof "$vm_bundle/Disk.img" >/dev/null 2>&1; then
    echo 'vm_clone=failed: source disk remained open; VM will be restarted' >&2
    exit 1
  fi
  mkdir -m 700 "$vm_clone"
  for name in Disk.img AuxiliaryStorage HardwareModel MachineIdentifier Parameters.txt; do
    [[ -f "$vm_bundle/$name" ]] || { echo "vm_clone=failed: missing $name" >&2; exit 1; }
    cp -c -p "$vm_bundle/$name" "$vm_clone/$name"
    [[ "$(stat -f %z "$vm_bundle/$name")" == "$(stat -f %z "$vm_clone/$name")" ]] || {
      echo "vm_clone=failed: size mismatch $name" >&2; exit 1;
    }
  done
  touch "$vm_clone/.complete"
  chmod 600 "$vm_clone/.complete"
  chmod 700 "$vm_clone"
  echo "vm_clone=created path=$vm_clone"
  exit 0
fi

if [[ "$mode" == vm-clone-drop ]]; then
  [[ -d "$vm_clone" && ! -L "$vm_clone" && -f "$vm_clone/Disk.img" ]] || {
    echo 'vm_clone_drop=refused: expected clone is absent' >&2; exit 1;
  }
  if lsof "$vm_clone/Disk.img" >/dev/null 2>&1; then
    echo 'vm_clone_drop=refused: clone disk is open' >&2; exit 1
  fi
  rm -f "$vm_clone/Disk.img" "$vm_clone/AuxiliaryStorage" "$vm_clone/HardwareModel" \
    "$vm_clone/MachineIdentifier" "$vm_clone/Parameters.txt" "$vm_clone/.complete"
  rmdir "$vm_clone"
  echo 'vm_clone_drop=removed'
  exit 0
fi

if [[ "$mode" == pack ]]; then
  VOS_CUA_TRACE_DIR="$root" "$repo_dir/tools/computer-dataset.sh" >/dev/null
  VOS_CUA_TRACE_DIR="$root/journeys" "$repo_dir/tools/computer-dataset.sh" >/dev/null
fi

python3 - "$mode" "$root" <<'PY'
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

mode = sys.argv[1]
root = Path(sys.argv[2]).resolve()

def digest(blob):
    return hashlib.sha256(blob).hexdigest()

def add_bytes(archive, name, blob):
    info = tarfile.TarInfo(name)
    info.size = len(blob)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(blob))

def approved_records(source, directory):
    events = {item['id']: item for item in
              (json.loads(line) for line in (directory / 'steps.jsonl').read_text().splitlines())}
    reviews = {item['id']: item for item in
               (json.loads(line) for line in (directory / 'reviews.jsonl').read_text().splitlines())}
    records = []
    files = {}
    for line in (directory / 'dataset.jsonl').read_text().splitlines():
        row = json.loads(line)
        trace_id = row.get('id', '')
        if not re.fullmatch(r'[0-9a-f]{32}', trace_id):
            raise ValueError('Invalid reviewed trace ID')
        event = events[trace_id]
        review = reviews[trace_id]
        if not (review.get('decision') == 'accept' and event.get('verified') is True
                and event.get('executed') is True and event.get('action') == 'click'
                and review.get('frame_sha256') == event.get('frame_sha256')
                and review.get('after_sha256') == event.get('after_sha256')):
            raise ValueError('Reviewed trace validation failed')
        before = f'frames/{trace_id}.jpg'
        after = f'frames/{trace_id}-after.jpg'
        if row.get('image') != before or event.get('after_frame') != after:
            raise ValueError('Reviewed image path mismatch')
        for relative, expected in ((before, event['frame_sha256']),
                                   (after, event['after_sha256'])):
            blob = (directory / relative).read_bytes()
            if digest(blob) != expected:
                raise ValueError('Reviewed image hash mismatch')
            files[f'{source}/{relative}'] = blob
        enriched = dict(row)
        enriched['source'] = source
        enriched['image'] = f'{source}/{before}'
        enriched['after_image'] = f'{source}/{after}'
        enriched['after_sha256'] = event['after_sha256']
        records.append(enriched)
    return records, files

if mode == 'pack':
    dock, dock_files = approved_records('dock', root)
    journey, journey_files = approved_records('journeys', root / 'journeys')
    all_records = dock + journey
    if not all_records:
        raise ValueError('No reviewed examples to archive')
    files = dict(dock_files)
    files.update(journey_files)
    manifest = {'format': 'virtualos-reviewed-computer-use-v1',
                'counts': {'dock': len(dock), 'journeys': len(journey)},
                'examples': len(all_records), 'images': len(files)}
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', compresslevel=9, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            add_bytes(archive, 'manifest.json', json.dumps(manifest, sort_keys=True).encode())
            dataset = b''.join(json.dumps(row, sort_keys=True, separators=(',', ':')).encode() + b'\n'
                               for row in all_records)
            add_bytes(archive, 'dataset.jsonl', dataset)
            for name, blob in sorted(files.items()):
                add_bytes(archive, name, blob)
    payload = buffer.getvalue()
    if len(payload) > 25_000_000:
        raise ValueError('Curated archive exceeds the 25 MB upload limit')
    sha = digest(payload)
    output = Path('/private/tmp') / ('vos-approved-' + sha + '.tar.gz')
    if output.exists() and digest(output.read_bytes()) != sha:
        raise ValueError('Existing package hash mismatch')
    output.write_bytes(payload)
    output.chmod(0o600)
    print(json.dumps({'path': str(output), 'examples': len(all_records),
                      'images': len(files), 'compressed_bytes': len(payload), 'sha256': sha}))
elif mode == 'record-receipt':
    incoming = Path('/private/tmp/vos-r2-receipt.json')
    receipt = json.loads(incoming.read_text())
    sha = receipt.get('sha256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', sha) or receipt.get('bucket') != 'mac-mini-computer-use-backups' or receipt.get('key') != 'approved/' + sha + '.tar.gz' or receipt.get('download_verified') is not True:
        raise ValueError('Invalid R2 receipt')
    with tempfile.NamedTemporaryFile('w', dir=root, prefix='.r2-receipt-', suffix='.json', delete=False) as stream:
        os.chmod(stream.name, 0o600)
        json.dump(receipt, stream, sort_keys=True)
        temporary = stream.name
    os.replace(temporary, root / 'r2-receipt.json')
    incoming.unlink()
    print(json.dumps({'receipt_saved': True, 'sha256': sha}))
elif mode == 'receipt':
    print((root / 'r2-receipt.json').read_text())
elif mode == 'discard-package':
    receipt = json.loads((root / 'r2-receipt.json').read_text())
    sha = receipt.get('sha256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', sha) or receipt.get('download_verified') is not True:
        raise ValueError('Invalid R2 receipt')
    package = Path('/private/tmp') / ('vos-approved-' + sha + '.tar.gz')
    if package.exists():
        if digest(package.read_bytes()) != sha:
            raise ValueError('Temporary package hash mismatch')
        package.unlink()
    print(json.dumps({'temporary_package_removed': True, 'sha256': sha}))
elif mode == 'prune':
    if subprocess.run(['pgrep', '-f', '[v]osctl computer-night'],
                      capture_output=True).returncode == 0:
        raise ValueError('Stop the guest practice runner before pruning')
    receipt = json.loads((root / 'r2-receipt.json').read_text())
    if receipt.get('download_verified') is not True:
        raise ValueError('R2 receipt lacks download verification')
    rows = [json.loads(line) for line in (root / 'steps.jsonl').read_text().splitlines()]
    reviews = {item['id']: item for item in
               (json.loads(line) for line in (root / 'reviews.jsonl').read_text().splitlines())}
    keep = {trace_id for trace_id, review in reviews.items()
            if review.get('decision') == 'accept'}
    keep.update(item['id'] for item in rows
                if item.get('executed') and item.get('verified') is not True)
    keep.update(item['id'] for item in rows[-10:])
    for target in {item.get('target_app') for item in rows}:
        success = [item for item in rows if item.get('target_app') == target
                   and item.get('executed') and item.get('verified') is True]
        keep.update(item['id'] for item in success[-3:])
    removed_count = removed_bytes = 0
    for path in (root / 'frames').glob('*.jpg'):
        match = re.fullmatch(r'([0-9a-f]{32})(?:-after)?\.jpg', path.name)
        if not match or match.group(1) in keep or path.is_symlink():
            continue
        removed_bytes += path.stat().st_size
        path.unlink()
        removed_count += 1
    print(json.dumps({'removed_redundant_frames': removed_count,
                      'freed_bytes': removed_bytes, 'retained_trace_ids': len(keep),
                      'r2_receipt': receipt['sha256']}))
PY
