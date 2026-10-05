#!/usr/bin/env bash
# fs-inspect.sh — read-only filesystem understanding toolkit.
#
# Implements the manifesto theme "Filesystems as first-class citizens"
# (MANIFESTO.md): RAW and sparse RAW, APFS containers and snapshots, the
# ASIF direction, and read-only bases plus copy-on-write overlays.
#
# This tool NEVER writes. It never attaches, mounts, or modifies anything.
# It refuses to inspect the production VM disk
# (~/VirtualOSMachines/virtualOS.bundle/Disk.img) or any path inside a
# `.bundle` unless --allow-production is explicitly passed. Even then it
# remains strictly read-only; it does not attach or mount anything.
#
# Usage:
#   ./tools/fs-inspect.sh [--allow-production] [--no-hash] <path> [overlay ...]
#
# <path> may be a disk image file OR a mounted filesystem path.
# Additional overlay/checkpoint files are reported conceptually (base+overlay).

set -euo pipefail

PROD_DISK="${HOME}/VirtualOSMachines/virtualOS.bundle/Disk.img"
MIB=$((1024 * 1024))

allow_production=0
do_hash=1
positionals=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --allow-production) allow_production=1 ;;
    --no-hash) do_hash=0 ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do positionals+=("$1"); shift; done
      break
      ;;
    -*)
      echo "fs-inspect: unknown option: $1" >&2
      exit 2
      ;;
    *)
      positionals+=("$1")
      ;;
  esac
  shift
done

if [[ ${#positionals[@]} -eq 0 ]]; then
  echo "Usage: ./tools/fs-inspect.sh [--allow-production] [--no-hash] <path> [overlay ...]" >&2
  exit 2
fi

have() { command -v "$1" >/dev/null 2>&1; }

section() {
  printf '\n== %s ==\n' "$1"
}

bytes_human() {
  local n="$1"
  if [[ ! "$n" =~ ^[0-9]+$ ]]; then
    printf '%s' "$n"
    return
  fi
  awk -v n="$n" 'BEGIN {
    split("B KiB MiB GiB TiB PiB", u, " ");
    i = 1;
    while (n >= 1024 && i < 6) { n /= 1024; i++ }
    printf "%.2f %s", n, u[i]
  }'
}

# ---- safety guard ----------------------------------------------------------
# Resolve a path to a canonical absolute form without requiring it to exist.
resolve_path() {
  local p="$1"
  if [[ "$p" != /* ]]; then
    p="${PWD}/${p}"
  fi
  if [[ -d "$p" ]]; then
    # realpath -m is unavailable on macOS by default; use a physical resolve
    # for directories, fall back to the input for non-existent paths.
    (cd "$p" 2>/dev/null && pwd -P) || printf '%s' "$p"
  else
    local dir base
    dir="$(dirname "$p")"
    base="$(basename "$p")"
    if [[ -d "$dir" ]]; then
      printf '%s/%s' "$(cd "$dir" && pwd -P)" "$base"
    else
      printf '%s' "$p"
    fi
  fi
}

guard_path() {
  local p="$1"
  local resolved
  resolved="$(resolve_path "$p")"

  local prod_resolved
  prod_resolved="$(resolve_path "$PROD_DISK")"

  local reason=""
  if [[ "$resolved" == "$prod_resolved" ]]; then
    reason="it is the production VM disk (${PROD_DISK})"
  elif [[ "$resolved" == *.bundle || "$resolved" == *.bundle/* ]]; then
    reason="it is inside a .bundle (${resolved})"
  elif [[ -n "${HOME:-}" ]]; then
    local prod_dir
    prod_dir="$(dirname "$prod_resolved")"
    if [[ "$resolved" == "$prod_dir" || "$resolved" == "$prod_dir/"* ]]; then
      reason="it is inside the production VM bundle directory (${prod_dir})"
    fi
  fi

  if [[ -n "$reason" ]]; then
    if [[ "$allow_production" -ne 1 ]]; then
      cat >&2 <<EOF
fs-inspect: refusing to inspect '$p'
fs-inspect: reason: ${reason}
fs-inspect: safety guard: the production disk is read-only-sacred.
fs-inspect: this tool is read-only even when allowed, but inspecting a live
fs-inspect: VM bundle requires an explicit opt-in. Re-run with:
fs-inspect:     ./tools/fs-inspect.sh --allow-production '$p'
fs-inspect: no files were read or written.
EOF
      exit 3
    fi
    echo "fs-inspect: WARNING: --allow-production set; ${reason}."
    echo "fs-inspect: this run stays strictly read-only (no attach, no mount, no write)."
  fi
}

# ---- 1. file-level facts ---------------------------------------------------
report_file_facts() {
  local p="$1"
  section "file facts"

  echo "\$ ls -l@ '${p}'"
  ls -l@ "$p" 2>&1 || true

  echo
  echo "\$ stat -f '%z %b %k' '${p}'  # size, 512B blocks, optimal io"
  local out logical blocks blksize allocated
  out="$(stat -f '%z %b %k' "$p" 2>/dev/null || true)"
  echo "$out"
  logical="$(echo "$out" | awk '{print $1}')"
  blocks="$(echo "$out" | awk '{print $2}')"
  blksize="$(echo "$out" | awk '{print $3}')"

  if [[ "$logical" =~ ^[0-9]+$ && "$blocks" =~ ^[0-9]+$ ]]; then
    allocated=$((blocks * 512))
    echo
    echo "logical size    : ${logical} B ($(bytes_human "$logical"))"
    echo "allocated blocks: ${allocated} B ($(bytes_human "$allocated")) across ${blocks} x 512B blocks"
    echo "block size hint : ${blksize} B"
    if (( allocated < logical )); then
      local delta=$((logical - allocated))
      local pct
      pct="$(awk -v d="$delta" -v l="$logical" 'BEGIN { if (l>0) printf "%.1f", 100*d/l; else print "0" }')"
      echo "sparse          : YES — ${delta} B ($(bytes_human "$delta"), ${pct}%) not physically allocated"
    else
      echo "sparse          : no (allocated >= logical)"
    fi
  else
    echo "(could not parse stat output)"
  fi

  echo
  echo "\$ xattr -l '${p}'  # sparse/compression flag candidates"
  if have xattr; then
    if ! xattr -l "$p" 2>/dev/null | grep -E '.' >/dev/null 2>&1; then
      echo "(no extended attributes)"
    else
      xattr -l "$p" 2>/dev/null || true
    fi
    # Explicitly probe the known APFS compression xattr.
    if xattr "$p" 2>/dev/null | grep -q '^com.apple.decmpfs$'; then
      echo "note: com.apple.decmpfs present (APFS compressed file)"
    fi
    if xattr "$p" 2>/dev/null | grep -q '^com.apple.ResourceFork$'; then
      echo "note: com.apple.ResourceFork present (compressed data may live here)"
    fi
  fi

  echo
  echo "\$ file '${p}'"
  file "$p" 2>&1 || true
}

# ---- 2. image type detection ----------------------------------------------
report_image_type() {
  local p="$1"
  section "image type"

  echo "\$ file '${p}'"
  file "$p" 2>&1 || true

  if have diskutil; then
    echo
    echo "\$ diskutil image info '${p}'  # read-only; tolerated if unsupported"
    if ! diskutil image info "$p" 2>&1; then
      echo "(diskutil image info failed or unsupported on this macOS; continuing)"
    fi
  fi

  echo
  echo "-- heuristic verdict --"
  local fdesc head_bytes
  fdesc="$(file "$p" 2>/dev/null || true)"
  # Read only a small header when the file is a regular file.
  if [[ -f "$p" ]]; then
    head_bytes="$(dd if="$p" bs=512 count=2 2>/dev/null | LC_ALL=C tr -d '\0' | cut -c1-64 || true)"
  fi
  case "$fdesc" in
    *APFS*) echo "likely: APFS image (file(1) recognized an APFS container)" ;;
    *"Apple File System"*) echo "likely: APFS image" ;;
    *ASIF*|*"Apple Sparse Image"*) echo "likely: ASIF (Apple sparse image format)" ;;
    *)
      if [[ -n "${head_bytes:-}" && "$head_bytes" == *"NXSB"* ]]; then
        echo "likely: APFS container (NXSB magic in header)"
      elif [[ "$head_bytes" == *"GPT"* || "$head_bytes" == *"EFI PART"* ]]; then
        echo "likely: RAW disk image with GPT partitioning"
      else
        echo "likely: RAW disk image (no APFS/ASIF signature in sampled header)"
      fi
      ;;
  esac
  echo "note: ASIF detection is heuristic; macOS 26 is moving toward ASIF support."
}

# ---- 3. hdiutil imageinfo --------------------------------------------------
report_hdiutil() {
  local p="$1"
  section "hdiutil imageinfo (read-only, no attach)"
  if ! have hdiutil; then
    echo "(hdiutil not available)"
    return
  fi
  # `imageinfo` only reads metadata; it never attaches. Some formats (e.g.
  # APFS/ASIF) may be rejected — tolerate failure.
  echo "\$ hdiutil imageinfo '${p}'"
  local out
  if ! out="$(hdiutil imageinfo "$p" 2>&1)"; then
    echo "$out"
    echo "(hdiutil imageinfo failed or format unsupported; continuing read-only)"
    return
  fi
  echo "$out" | grep -E '^(Format|Format Description|Partition Name|Partition Type|Partition Size|Total Bytes|Class Name|partition-scheme|Total Sectors)' || echo "$out"

  echo
  echo "-- partition summary --"
  if have hdiutil; then
    hdiutil imageinfo "$p" 2>/dev/null \
      | awk '/^[[:space:]]*Partition [0-9]+:/{p=1} p{print}' \
      | head -40 || true
  fi
}

# ---- 4. mounted path facts -------------------------------------------------
report_mounted() {
  local p="$1"
  section "mounted filesystem facts"

  echo "\$ df -h '${p}'"
  df -h "$p" 2>&1 || true

  echo
  echo "\$ mount | grep -F '${p}'"
  local mp
  mp="$(df "$p" 2>/dev/null | awk 'NR==2 {print $NF}')"
  if [[ -n "$mp" ]]; then
    mount | grep -F "$mp" 2>&1 || true
  else
    mount | grep -F "$p" 2>&1 || true
  fi

  if have diskutil; then
    # diskutil info accepts a device node or a mount point, not an arbitrary
    # path. Prefer the resolved mount point, then the device, then the path.
    local dev mp target info_out
    dev="$(df "$p" 2>/dev/null | awk 'NR==2 {print $1}')"
    target="$p"
    [[ -n "$dev" && "$dev" == /dev/* ]] && target="$dev"

    echo
    echo "\$ diskutil info '${target}'"
    info_out="$(diskutil info "$target" 2>&1 || true)"
    if echo "$info_out" | grep -qE 'Device Node|Volume Name|File System|Type \(Bundle\)|Mount Point|Volume Free Space|Container Total Space|Container Free Space|Read-Only|APFS'; then
      echo "$info_out" | grep -E 'Device Node|Volume Name|File System|Type \(Bundle\)|Mount Point|Volume Free Space|Container Total Space|Container Free Space|Read-Only|APFS'
      # Also show mount point when the target was a device node.
      mp="$(echo "$info_out" | awk -F: '/^ *Mount Point/ {gsub(/^ +/,"",$2); print $2}' | head -1)"
      if [[ -n "$mp" ]]; then
        echo
        echo "\$ diskutil info '${mp}'  # mount-point view"
        diskutil info "$mp" 2>&1 | grep -E 'Device Node|Volume Name|File System|Mount Point|Volume Free Space|Container Total Space|Container Free Space|Read-Only|APFS' || true
      fi
    else
      echo "$info_out"
      echo "(diskutil info produced no matching lines)"
    fi

    echo
    echo "\$ diskutil apfs list  # filtered to the relevant container"
    local apfs_lines
    apfs_lines="$(diskutil apfs list 2>/dev/null || true)"
    if [[ -z "$apfs_lines" ]]; then
      echo "(diskutil apfs list unavailable)"
    else
      # Extract the container reference (e.g. "disk3") from either the
      # "APFS Container:" line or a "Part of Whole:" line, then print that
      # container's full section from `diskutil apfs list`.
      local container vol_dev
      container="$(echo "$info_out" | awk -F: '/^ *APFS Container:/ {gsub(/ /,"",$2); print $2}' | head -1)"
      [[ -z "$container" ]] && container="$(echo "$info_out" | awk -F: '/Part of Whole/ {gsub(/ /,"",$2); print "disk"$2}' | head -1)"
      vol_dev="$(echo "$info_out" | awk -F: '/Device Node/ {gsub(/ /,"",$2); print $2}' | head -1)"
      if [[ -n "$container" ]]; then
        echo "$apfs_lines" | awk -v s="$container" 'BEGIN{f=0} /^\+--/ {f=0} index($0, s" "){f=1} f{print}' | head -45
      elif [[ -n "$vol_dev" ]]; then
        echo "$apfs_lines" | grep -F "$vol_dev" -A2 -B2 | head -45
      else
        echo "$apfs_lines" | grep -E 'APFS Container|Capacity|Free|Snapshot|Physical Store' | head -30
      fi
    fi
  fi
}

# ---- 5. base + overlay demonstration --------------------------------------
report_overlay() {
  local base="$1"
  shift
  section "base + overlay / checkpoint concept"
  echo "base image : ${base}"
  echo "The manifesto model is a verified read-only base plus a copy-on-write"
  echo "overlay or checkpoint. The base never changes; only the overlay records"
  echo "new writes, so one image can serve many machines."

  if [[ $# -eq 0 ]]; then
    echo
    echo "(no overlay/checkpoint paths supplied; pass extra args to inspect them)"
    return
  fi

  local ov
  for ov in "$@"; do
    echo
    echo "-- overlay candidate: ${ov}"
    if [[ ! -e "$ov" ]]; then
      echo "   (does not exist)"
      continue
    fi
    local osize
    osize="$(stat -f '%z' "$ov" 2>/dev/null || echo '?')"
    echo "   size      : ${osize} B ($(bytes_human "$osize"))"
    local ost
    ost="$(stat -f '%z %b' "$ov" 2>/dev/null || true)"
    local olog oblk
    olog="$(echo "$ost" | awk '{print $1}')"
    oblk="$(echo "$ost" | awk '{print $2}')"
    if [[ "$olog" =~ ^[0-9]+$ && "$oblk" =~ ^[0-9]+$ ]]; then
      local oalloc=$((oblk * 512))
      echo "   allocated : ${oalloc} B ($(bytes_human "$oalloc"))"
      if (( oalloc < olog )); then
        echo "   sparse    : YES"
      else
        echo "   sparse    : no"
      fi
    fi
    if [[ "$do_hash" -eq 1 ]] && have shasum; then
      echo "   sha256    : (capped at first 64 MiB; full hash not computed)"
      local hash
      hash="$(head -c $((64 * MIB)) "$ov" 2>/dev/null | shasum -a 256 2>/dev/null | awk '{print $1}')"
      local full
      full="$(stat -f '%z' "$ov" 2>/dev/null || echo 0)"
      if [[ "$full" -gt $((64 * MIB)) ]]; then
        echo "   sha256[0:64MiB]: ${hash}"
      else
        echo "   sha256    : ${hash}"
      fi
    fi
  done
}

# ---- main ------------------------------------------------------------------
main_path="${positionals[0]}"
overlays=()
if [[ ${#positionals[@]} -gt 1 ]]; then
  overlays=("${positionals[@]:1}")
fi

guard_path "$main_path"

section "target"
echo "path      : ${main_path}"
resolved="$(resolve_path "$main_path")"
echo "resolved  : ${resolved}"

if [[ -f "$main_path" ]]; then
  report_file_facts "$main_path"
  report_image_type "$main_path"
  report_hdiutil "$main_path"
elif [[ -d "$main_path" ]]; then
  report_file_facts "$main_path"
  report_mounted "$main_path"
else
  echo "fs-inspect: path does not exist: ${main_path}" >&2
  exit 1
fi

report_overlay "$main_path" ${overlays[@]+"${overlays[@]}"}

section "safety"
echo "read-only: yes — no writes, no attach, no mount, no modification of any path."
echo "production disk reference: ${PROD_DISK}"
echo "done."
