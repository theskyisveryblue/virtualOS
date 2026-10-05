#!/usr/bin/env bash
# container-inspect.sh — read-only inspection of the local container runtime(s).
#
# Implements the manifesto theme "Virtualization and containerization to do the
# work" (MANIFESTO.md): a container is a process with a drawn boundary — cheap,
# fast, dense — beside the full-machine VM boundary and the nested-VM boundary.
#
# This tool NEVER mutates state. It never starts, stops, creates, runs, pulls,
# removes, or networks a container or a runtime service. Every runtime call it
# makes is a read-only status/list/version query. If a runtime is missing it
# prints a clear "not installed" line and continues. No network access.
#
# Usage:
#   ./tools/container-inspect.sh [--json] [--lines N]
#
#   --json      emit a single machine-readable JSON object instead of text
#   --lines N   max list/ps entries to show per runtime (default 5)
#   -h, --help  print usage

set -euo pipefail

JSON=0
LINES=5

usage() {
  sed -n '2,17p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1 ;;
    --lines)
      shift
      if [[ $# -eq 0 || ! "$1" =~ ^[0-9]+$ ]]; then
        echo "container-inspect: --lines requires a number" >&2
        exit 2
      fi
      LINES="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "container-inspect: unknown option: $1" >&2
      echo "Usage: ./tools/container-inspect.sh [--json] [--lines N]" >&2
      exit 2
      ;;
  esac
  shift
done

have() { command -v "$1" >/dev/null 2>&1; }

# jstr <string> — quote a string as a JSON scalar. Uses python3 when available
# (correct escaping for control chars/unicode); falls back to an awk escaper.
jstr() {
  if have python3; then
    printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
  else
    printf '%s' "$1" | awk 'BEGIN{printf "\""} {
      gsub(/\\/,"\\\\"); gsub(/"/,"\\\""); gsub(/\t/,"\\t");
      gsub(/\r/,"\\r"); gsub(/\n/,"\\n");
      printf "%s", $0
    } END{print "\""}'
  fi
}

json_str_array() {
  # args are already-quoted JSON strings
  local out="[" first=1 a
  for a in "$@"; do
    [[ $first -eq 1 ]] || out+=","
    first=0
    out+="$a"
  done
  out+="]"
  printf '%s' "$out"
}

# ---- text-mode section helper ---------------------------------------------
section() { printf '\n== %s ==\n' "$1"; }

# ============================================================================
# Runtime probes — all read-only.
# ============================================================================

# Global accumulators for JSON.
declare -a RT_NAMES=()
declare -a RT_BIN=()
declare -a RT_VER=()
declare -a RT_DAEMON_UP=()   # yes|no|unknown|n/a
declare -a RT_LIST_N=()      # total items (or empty)
declare -a RT_LIST_ERR=()
declare -a RT_SAMPLE=()      # newline-joined first N entries

# probe_runtime <name> <binary>
# Runs the runtime's own read-only version/status/list queries and records the
# results in the global accumulators. Read-only by convention.
probe_runtime() {
  local name="$1" bin="$2"
  if ! have "$bin"; then
    RT_NAMES+=("$name"); RT_BIN+=(""); RT_VER+=("")
    RT_DAEMON_UP+=("n/a"); RT_LIST_N+=(""); RT_LIST_ERR+=("not installed")
    RT_SAMPLE+=("")
    return
  fi
  RT_NAMES+=("$name")
  RT_BIN+=("$(command -v "$bin")")

  # version
  local ver=""
  case "$name" in
    container) ver="$(container --version 2>/dev/null | head -1 || true)" ;;
    docker)    ver="$("$bin" version --format 'client={{.Client.Version}} server={{.Server.Version}}' 2>/dev/null | head -1 || true)" ;;
    podman)    ver="$("$bin" --version 2>/dev/null | head -1 || true)" ;;
    nerdctl)   ver="$("$bin" --version 2>/dev/null | head -1 || true)" ;;
    colima)    ver="$("$bin" version 2>/dev/null | head -1 || true)" ;;
  esac
  RT_VER+=("$ver")

  # daemon / service status — read-only
  local up="unknown"
  case "$name" in
    container)
      if container system status >/dev/null 2>&1; then up="yes"; else up="no"; fi ;;
    docker)
      if docker info >/dev/null 2>&1; then up="yes"; else up="no"; fi ;;
    podman)
      if podman info >/dev/null 2>&1; then up="yes"; else up="no"; fi ;;
    nerdctl)
      if nerdctl info >/dev/null 2>&1; then up="yes"; else up="no"; fi ;;
    colima)
      if colima status >/dev/null 2>&1; then up="yes"; else up="no"; fi ;;
  esac
  # If the runtime's own version query failed entirely, leave unknown.
  if [[ -z "$ver" && "$name" != "colima" ]]; then
    [[ "$up" == "yes" ]] || up="unknown"
  fi
  RT_DAEMON_UP+=("$up")

  # list / ps — read-only, capped
  local raw="" lerr=""
  case "$name" in
    container)
      if ! raw="$(container list 2>&1)"; then lerr="$raw"; raw=""; fi ;;
    docker)
      if ! raw="$(docker ps --format '{{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}' 2>&1)"; then lerr="$raw"; raw=""; fi ;;
    podman)
      if ! raw="$(podman ps --format '{{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}' 2>&1)"; then lerr="$raw"; raw=""; fi ;;
    nerdctl)
      if ! raw="$(nerdctl ps --format '{{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}' 2>&1)"; then lerr="$raw"; raw=""; fi ;;
    colima)
      # colima has no container list; its "list" is the set of colima instances.
      if ! raw="$(colima list 2>&1)"; then lerr="$raw"; raw=""; fi ;;
  esac

  if [[ -n "$lerr" ]]; then
    RT_LIST_N+=(""); RT_LIST_ERR+=("$(printf '%s' "$lerr" | head -1)")
    RT_SAMPLE+=("")
  else
    # strip a header line where the runtime prints one.
    local body
    body="$raw"
    case "$name" in
      container) body="$(printf '%s\n' "$raw" | sed '1{/^ID[[:space:]]/d;}')" ;;
      docker|podman|nerdctl) : ;; # --format had no header
      colima) body="$(printf '%s\n' "$raw" | sed '1{/^PROFILE[[:space:]]/d;}')" ;;
    esac
    body="$(printf '%s\n' "$body" | sed '/^[[:space:]]*$/d')"
    local n
    if [[ -z "$body" ]]; then n=0; else n="$(printf '%s\n' "$body" | wc -l | tr -d ' ')"; fi
    local sample
    sample="$(printf '%s\n' "$body" | head -n "$LINES")"
    RT_LIST_N+=("$n"); RT_LIST_ERR+=(""); RT_SAMPLE+=("$sample")
  fi
}

probe_all() {
  probe_runtime container container ""
  probe_runtime docker    docker    ""
  probe_runtime podman    podman    ""
  probe_runtime nerdctl   nerdctl   ""
  probe_runtime colima    colima    ""
}

# ============================================================================
# Machine facts
# ============================================================================
mach_ncpu="$(sysctl -n hw.ncpu 2>/dev/null || echo unknown)"
mach_mem_bytes="$(sysctl -n hw.memsize 2>/dev/null || echo '')"
mach_hv="$(sysctl -n kern.hv_support 2>/dev/null || echo unknown)"
mach_os_name="$(sw_vers -productName 2>/dev/null || echo unknown)"
mach_os_ver="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
mach_os_build="$(sw_vers -buildVersion 2>/dev/null || echo unknown)"

mem_human() {
  local n="$1"
  if [[ ! "$n" =~ ^[0-9]+$ ]]; then printf '%s' "$n"; return; fi
  awk -v n="$n" 'BEGIN {
    split("B KiB MiB GiB TiB", u, " ");
    i = 1; while (n >= 1024 && i < 5) { n /= 1024; i++ }
    printf "%.2f %s", n, u[i]
  }'
}

probe_all

# ============================================================================
# JSON output
# ============================================================================
if [[ "$JSON" -eq 1 ]]; then
  rt_items=""
  first=1
  for i in "${!RT_NAMES[@]}"; do
    [[ $first -eq 1 ]] || rt_items+=","
    first=0
    name="${RT_NAMES[$i]}"
    if [[ -z "${RT_BIN[$i]}" ]]; then
      installed=false
    else
      installed=true
    fi

    case "${RT_DAEMON_UP[$i]}" in
      yes) up_val=true ;;
      no)  up_val=false ;;
      *)   up_val=null ;;
    esac

    if [[ -n "${RT_LIST_N[$i]}" ]]; then
      list_n="${RT_LIST_N[$i]}"
    else
      list_n=null
    fi

    sample_json="[]"
    if [[ -n "${RT_SAMPLE[$i]}" ]]; then
      declare -a sample_arr=()
      while IFS= read -r line; do
        [[ -n "$line" ]] && sample_arr+=("$(jstr "$line")")
      done <<< "${RT_SAMPLE[$i]}"
      if [[ ${#sample_arr[@]} -gt 0 ]]; then
        sample_json="$(json_str_array "${sample_arr[@]}")"
      fi
    fi

    rt_items+="{\"name\":$(jstr "$name"),"
    rt_items+="\"binary\":$(jstr "${RT_BIN[$i]}"),"
    rt_items+="\"installed\":${installed},"
    rt_items+="\"version\":$(jstr "${RT_VER[$i]}"),"
    rt_items+="\"daemon_running\":${up_val},"
    rt_items+="\"entry_count\":${list_n},"
    rt_items+="\"list_error\":$(jstr "${RT_LIST_ERR[$i]}"),"
    rt_items+="\"sample\":${sample_json}}"
  done

  if [[ "$mach_mem_bytes" =~ ^[0-9]+$ ]]; then
    mem_json="$mach_mem_bytes"
  else
    mem_json=null
  fi
  if [[ "$mach_ncpu" =~ ^[0-9]+$ ]]; then
    cpu_json="$mach_ncpu"
  else
    cpu_json=null
  fi
  case "$mach_hv" in
    1) hv_json=true ;;
    0) hv_json=false ;;
    *) hv_json=null ;;
  esac

  printf '{\n'
  printf '  "tool": "container-inspect",\n'
  printf '  "read_only": true,\n'
  printf '  "runtimes": [%s],\n' "$rt_items"
  printf '  "machine": {\n'
  printf '    "cpu_count": %s,\n' "$cpu_json"
  printf '    "memory_bytes": %s,\n' "$mem_json"
  printf '    "hv_support": %s,\n' "$hv_json"
  printf '    "os": {"name":%s,"version":%s,"build":%s}\n' \
    "$(jstr "$mach_os_name")" "$(jstr "$mach_os_ver")" "$(jstr "$mach_os_build")"
  printf '  }\n'
  printf '}\n'
  exit 0
fi

# ============================================================================
# Text output
# ============================================================================
echo "container-inspect — read-only local container runtime report"
echo "read-only: yes — no start/stop/create/run/pull/rm, no network."

section "runtimes"
for i in "${!RT_NAMES[@]}"; do
  name="${RT_NAMES[$i]}"
  printf '\n-- %s --\n' "$name"
  if [[ -z "${RT_BIN[$i]}" ]]; then
    echo "status    : not installed"
    continue
  fi
  echo "binary    : ${RT_BIN[$i]}"
  [[ -n "${RT_VER[$i]}" ]] && echo "version   : ${RT_VER[$i]}" || echo "version   : (unavailable)"
  case "${RT_DAEMON_UP[$i]}" in
    yes) echo "service   : running" ;;
    no)  echo "service   : not running" ;;
    *)   echo "service   : unknown" ;;
  esac

  if [[ -n "${RT_LIST_ERR[$i]}" ]]; then
    echo "list      : (unavailable) ${RT_LIST_ERR[$i]}"
  else
    echo "entries   : ${RT_LIST_N[$i]} (showing up to ${LINES})"
    if [[ -n "${RT_SAMPLE[$i]}" ]]; then
      while IFS= read -r line; do
        printf '            %s\n' "$line"
      done <<< "${RT_SAMPLE[$i]}"
    else
      echo "            (none)"
    fi
  fi

  # Apple `container` framework specificity — tolerate unknown subcommands.
  if [[ "$name" == "container" ]]; then
    printf '\n   [Apple container framework specifics]\n'
    echo "   \$ container system status"
    if sys_out="$(container system status 2>&1)"; then
      printf '%s\n' "$sys_out" | sed 's/^/     /'
    else
      printf '     (unavailable) %s\n' "$(printf '%s' "$sys_out" | head -1)"
    fi
    echo "   \$ container list"
    if lst_out="$(container list 2>&1)"; then
      printf '%s\n' "$lst_out" | sed 's/^/     /'
    else
      printf '     (unavailable) %s\n' "$(printf '%s' "$lst_out" | head -1)"
    fi
    echo "   note: unknown/renamed 'container' subcommands are tolerated, not fatal."
  fi
done

section "machine resources"
echo "cpu count       : ${mach_ncpu}"
if [[ "$mach_mem_bytes" =~ ^[0-9]+$ ]]; then
  echo "memory          : ${mach_mem_bytes} B ($(mem_human "$mach_mem_bytes"))"
else
  echo "memory          : ${mach_mem_bytes:-unknown}"
fi
case "$mach_hv" in
  1) echo "kern.hv_support : 1 (hypervisor / Virtualization.framework supported)" ;;
  0) echo "kern.hv_support : 0 (hypervisor NOT supported)" ;;
  *) echo "kern.hv_support : ${mach_hv}" ;;
esac
echo "macOS           : ${mach_os_name} ${mach_os_ver} (${mach_os_build})"

section "safety"
echo "read-only: yes — only version/status/list/ps queries were issued."
echo "no container was started, stopped, created, run, pulled, or removed."
echo "no network requests were made."
echo "done."
