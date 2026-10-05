#!/bin/bash
#
# bin-inspect.sh - read-only arm64 / Mach-O binary inspection toolkit
#
# A practical wrapper around the standard macOS inspection tools:
#   file, lipo, otool, nm, codesign, swift-demangle
#
# This tool is for UNDERSTANDING binaries. It is strictly read-only:
# it never modifies, patches, resigns, or writes to the target. There is
# no network access. Optional sections must be explicitly requested.
#
# See tools/BIN_INSPECT.md for documentation.
#
# Usage: bin-inspect.sh [--disasm] [--strings] [--arch ARCH] [--max-lines N] <binary>
#

set -u

# ---------------------------------------------------------------------------
# Configuration / defaults
# ---------------------------------------------------------------------------
SCRIPT_NAME="$(basename "$0")"
TARGET=""
DO_DISASM=0
DO_STRINGS=0
WANT_ARCH=""
DISASM_LINES=120
STRING_LINES=200
NM_SYMBOL_CAP=40
LIB_CAP=60
RPATH_CAP=40
LOADCMD_LINE_CAP=400

# Colors only when stdout is a tty.
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
  C_KEY=$'\033[1;34m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'
else
  C_RESET=""; C_DIM=""; C_BOLD=""; C_KEY=""; C_WARN=""; C_ERR=""
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
${C_BOLD}${SCRIPT_NAME}${C_RESET} - read-only Mach-O / arm64 binary inspector

Usage:
  ${SCRIPT_NAME} [options] <binary>

Options:
  --disasm          Include a capped instruction disassembly (otool -tV).
  --strings         Include a capped printable-string list (otool -s __TEXT __cstring).
  --arch ARCH       Restrict to one architecture slice (arm64, arm64e, x86_64).
  --max-lines N     Max lines for disassembly/strings sections (default: ${DISASM_LINES} / ${STRING_LINES}).
  -h, --help        Show this help.

This tool is read-only and offline. It never modifies the target binary.
EOF
}

have() { command -v "$1" >/dev/null 2>&1; }

section() {
  printf '\n%s============================================================%s\n' "$C_KEY" "$C_RESET"
  printf '%s== %s%s\n' "$C_KEY" "$1" "$C_RESET"
  printf '%s============================================================%s\n' "$C_KEY" "$C_RESET"
}

note()  { printf '%s  - %s%s\n' "$C_DIM" "$1" "$C_RESET"; }
warn()  { printf '%s  ! %s%s\n' "$C_WARN" "$1" "$C_RESET"; }
err()   { printf '%s  x %s%s\n' "$C_ERR" "$1" "$C_RESET" >&2; }

die() { err "$1"; exit 1; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --disasm)   DO_DISASM=1; shift ;;
    --strings)  DO_STRINGS=1; shift ;;
    --arch)     WANT_ARCH="${2:-}"; shift 2 ;;
    --max-lines) DISASM_LINES="${2:-120}"; STRING_LINES="${2:-200}"; shift 2 ;;
    -h|--help)  usage; exit 0 ;;
    --*)        die "unknown option: $1" ;;
    *)
      if [ -z "$TARGET" ]; then TARGET="$1"; shift
      else die "unexpected extra argument: $1"; fi
      ;;
  esac
done

[ -n "$TARGET" ] || { usage; exit 2; }

# ---------------------------------------------------------------------------
# 1. Validate input
# ---------------------------------------------------------------------------
section "TARGET"
printf '  path      : %s\n' "$TARGET"

if [ ! -e "$TARGET" ]; then
  die "path does not exist: $TARGET"
fi
if [ -d "$TARGET" ]; then
  die "path is a directory, not a file: $TARGET (point at the executable inside a .app, e.g. Foo.app/Contents/MacOS/Foo)"
fi
if [ ! -r "$TARGET" ]; then
  die "path is not readable: $TARGET"
fi

if have file; then
  # On fat binaries file(1) prints one line per slice; take only the header line.
  FILE_OUT="$(file -b "$TARGET" 2>&1 | head -n 1)"
else
  die "'file' not found (unexpected on macOS)"
fi

printf '  file(1)   : %s\n' "$FILE_OUT"

case "$FILE_OUT" in
  *Mach-O*) : ;;
  *)
    die "not a Mach-O binary per file(1): $FILE_OUT"
    ;;
esac

if [ -L "$TARGET" ]; then
  warn "target is a symlink; inspecting resolved path contents"
fi

SIZE_BYTES="$(stat -f '%z' "$TARGET" 2>/dev/null || echo '?')"
printf '  size      : %s bytes\n' "$SIZE_BYTES"

# ---------------------------------------------------------------------------
# 2. Architectures (lipo)
# ---------------------------------------------------------------------------
section "ARCHITECTURES"
ARCHES=""
if have lipo; then
  LIPO_OUT="$(lipo -info "$TARGET" 2>&1)"
  printf '  lipo      : %s\n' "$LIPO_OUT"
  ARCHES="$(printf '%s' "$LIPO_OUT" | sed -n 's/.*are: //p')"
  if [ -z "$ARCHES" ]; then
    ARCHES="$(printf '%s' "$LIPO_OUT" | sed -n 's/.*architecture: //p')"
  fi
  case "$ARCHES" in
    *arm64e*) note "contains arm64e (pointer-authentication hardened slice)" ;;
  esac
  case "$ARCHES" in
    *arm64*)  note "contains arm64 (Apple silicon native)" ;;
  esac
  case "$ARCHES" in
    *x86_64*) note "contains x86_64 (Intel / Rosetta slice)" ;;
  esac
else
  warn "lipo not found"
fi

# Effective otool arch flag when the user requested one.
ARCH_FLAG=""
if [ -n "$WANT_ARCH" ]; then
  if printf '%s' "$ARCHES" | tr ' ' '\n' | grep -qx "$WANT_ARCH"; then
    ARCH_FLAG="-arch $WANT_ARCH"
    note "restricting otool/nm sections to arch: $WANT_ARCH"
  else
    warn "requested arch '$WANT_ARCH' not present; using default slice"
  fi
fi

# ---------------------------------------------------------------------------
# 3. Code signature + entitlements
# ---------------------------------------------------------------------------
section "CODE SIGNATURE"
if have codesign; then
  CS_DV="$(codesign -dv "$TARGET" 2>&1)"
  CS_STATUS=$?
  if printf '%s' "$CS_DV" | grep -qi 'code object is not signed'; then
    warn "unsigned (no code signature)"
  elif [ $CS_STATUS -ne 0 ] && [ -z "$CS_DV" ]; then
    warn "codesign could not read a signature (unsigned or malformed)"
  else
    printf '%s\n' "$CS_DV" | sed 's/^/  /'
  fi

  printf '\n  -- entitlements --\n'
  CS_ENT="$(codesign -d --entitlements - "$TARGET" 2>&1)"
  if printf '%s' "$CS_ENT" | grep -qi 'code object is not signed\|not signed at all'; then
    note "no entitlements (unsigned binary)"
  elif printf '%s' "$CS_ENT" | grep -qi 'no entitlements'; then
    note "signed, but no entitlements"
  elif [ -n "$CS_ENT" ]; then
    # Keep only the meaningful payload lines, drop diagnostic header noise.
    printf '%s\n' "$CS_ENT" | sed 's/^/  /' | head -n 80
  else
    note "no entitlement output"
  fi
else
  warn "codesign not found"
fi

# ---------------------------------------------------------------------------
# 4. Linked dylibs + rpaths (otool -L / otool -l)
# ---------------------------------------------------------------------------
section "LINKED LIBRARIES (otool -L)"
if have otool; then
  OT_L="$(otool $ARCH_FLAG -L "$TARGET" 2>/dev/null | tail -n +2)"
  if [ -n "$OT_L" ]; then
    LIB_COUNT=$(printf '%s\n' "$OT_L" | grep -c . )
    printf '%s\n' "$OT_L" | head -n "$LIB_CAP" | sed 's/^/  /'
    if [ "$LIB_COUNT" -gt "$LIB_CAP" ]; then
      note "... truncated ($LIB_COUNT total, showing $LIB_CAP)"
    fi
    printf '  --\n  total linked images: %s\n' "$LIB_COUNT"
  else
    note "no linked libraries reported"
  fi
else
  warn "otool not found"
fi

# We capture otool -l once and reuse it for load-command analysis.
OT_L_RAW=""
if have otool; then
  OT_L_RAW="$(otool $ARCH_FLAG -l "$TARGET" 2>/dev/null)"
fi

section "LOAD COMMANDS (selected)"
if [ -z "$OT_L_RAW" ]; then
  note "no load-command output"
else
  # Build a compact list of load command names in order.
  printf '  load command census:\n'
  printf '%s\n' "$OT_L_RAW" \
    | awk '/^Load command /{ if (name!="") count[name]++ } /^      cmd /{ name=$2 } END{ for (c in count) printf "    %-28s %d\n", c, count[c] }' \
    | sort

  printf '\n  -- LC_BUILD_VERSION / LC_VERSION_MIN_* (platform + min OS) --\n'
  printf '%s\n' "$OT_L_RAW" | awk '
    /cmd LC_BUILD_VERSION/ {print "    cmd: LC_BUILD_VERSION"; inb=1; next}
    /cmd LC_VERSION_MIN_MACOSX/ {print "    cmd: LC_VERSION_MIN_MACOSX"; inb=1; next}
    /cmd LC_VERSION_MIN_IPHONEOS/ {print "    cmd: LC_VERSION_MIN_IPHONEOS"; inb=1; next}
    inb && /^ *platform / {print "    platform : "$2; next}
    inb && /^ *minos / {print "    minos    : "$2; next}
    inb && /^ *sdk / {print "    sdk      : "$2; next}
    inb && /^ *version / {print "    version  : "$2; next}
    inb && /^ *ntools/ {inb=0}
  '
  if ! printf '%s\n' "$OT_L_RAW" | grep -q 'LC_BUILD_VERSION\|LC_VERSION_MIN'; then
    note "no build-version load command found"
  fi

  printf '\n  -- LC_RPATH --\n'
  RP_COUNT=$(printf '%s\n' "$OT_L_RAW" | grep -c 'LC_RPATH')
  if [ "$RP_COUNT" -eq 0 ]; then
    note "none declared"
  else
    printf '%s\n' "$OT_L_RAW" | awk '/cmd LC_RPATH/{r=1; next} r && /path /{sub(/^ *path /,""); sub(/ \(offset [0-9]+\)$/,""); print "    "$0; r=0}' | head -n "$RPATH_CAP"
    if [ "$RP_COUNT" -gt "$RPATH_CAP" ]; then
      note "... truncated ($RP_COUNT total, showing $RPATH_CAP)"
    fi
  fi

  printf '\n  -- LC_LOAD_DYLIB (raw names) --\n'
  printf '%s\n' "$OT_L_RAW" | awk '/cmd LC_LOAD_DYLIB/{d=1; next} d && /name /{print "    "$2; d=0}' | head -n "$LIB_CAP"
  printf '    (full list with versions is in LINKED LIBRARIES above)\n'

  printf '\n  -- LC_CODE_SIGNATURE --\n'
  printf '%s\n' "$OT_L_RAW" | awk '
    /cmd LC_CODE_SIGNATURE/ {s=1; print "    LC_CODE_SIGNATURE present"; next}
    s && /dataoff/ {print "    "$0; next}
    s && /datasize/ {print "    "$0; s=0}
  ' || true
  if ! printf '%s\n' "$OT_L_RAW" | grep -q 'LC_CODE_SIGNATURE'; then
    note "no LC_CODE_SIGNATURE (unsigned)"
  fi

  printf '\n  -- LC_UUID / LC_MAIN --\n'
  printf '%s\n' "$OT_L_RAW" | awk '
    /cmd LC_UUID/ {u=1; next}
    u && /uuid / {print "    UUID: "$2; u=0}
    /cmd LC_MAIN/ {m=1; print "    LC_MAIN present"; next}
    m && /entryoff/ {print "    "$0; next}
    m && /stacksize/ {print "    "$0; m=0}
  '

  # Guard against pathological output size (still read-only, just display).
  LCC=$(printf '%s\n' "$OT_L_RAW" | grep -c '^Load command')
  if [ "$LCC" -gt "$LOADCMD_LINE_CAP" ]; then
    note "many load commands ($LCC); detailed dump intentionally summarized"
  fi
fi

# ---------------------------------------------------------------------------
# 5. Symbol summary (nm -m)
# ---------------------------------------------------------------------------
section "SYMBOLS (nm -m, summarized)"
if have nm; then
  NM_RAW="$(nm $ARCH_FLAG -m "$TARGET" 2>/dev/null)"
  if [ -z "$NM_RAW" ]; then
    warn "nm produced no symbols (stripped binary?)"
  else
    TOTAL_SYM=$(printf '%s\n' "$NM_RAW" | grep -c .)
    UNDEF_SYM=$(printf '%s\n' "$NM_RAW" | grep -c 'undefined')
    DEF_SYM=$(( TOTAL_SYM - UNDEF_SYM ))
    OBJC_SYM=$(printf '%s\n' "$NM_RAW" | grep -c '_OBJC_')
    SWIFT_SYM=$(printf '%s\n' "$NM_RAW" | grep -Ec '\$s')
    printf '  total symbols : %s\n' "$TOTAL_SYM"
    printf '  defined       : %s\n' "$DEF_SYM"
    printf '  undefined     : %s\n' "$UNDEF_SYM"
    printf '  objc-ish      : %s\n' "$OBJC_SYM"
    printf '  swift-mangled : %s\n' "$SWIFT_SYM"

    printf '\n  -- sample defined (demangled where possible) --\n'
    DEMANGLE=""
    if command -v swift-demangle >/dev/null 2>&1; then
      DEMANGLE="swift-demangle"
    elif command -v xcrun >/dev/null 2>&1 && xcrun --find swift-demangle >/dev/null 2>&1; then
      DEMANGLE="xcrun swift-demangle"
    fi

    printf '%s\n' "$NM_RAW" | grep -v 'undefined' | head -n "$NM_SYMBOL_CAP" | while IFS= read -r line; do
      # strip the leading address if present
      sym="$(printf '%s' "$line" | awk '{print $NF}')"
      # Swift symbols appear as _$_s / _$s (the loader adds a leading underscore).
      if [ -n "$DEMANGLE" ] && printf '%s' "$sym" | grep -Eq '^_?\$s'; then
        dm="$($DEMANGLE -simplified "$sym" 2>/dev/null)"
        [ -n "$dm" ] && printf '    %s\n      => %s\n' "$sym" "$dm" || printf '    %s\n' "$sym"
      else
        printf '    %s\n' "$sym"
      fi
    done
    if [ "$DEF_SYM" -gt "$NM_SYMBOL_CAP" ]; then
      note "... truncated (showing $NM_SYMBOL_CAP of $DEF_SYM defined)"
    fi

    printf '\n  -- sample undefined imports --\n'
    printf '%s\n' "$NM_RAW" | grep 'undefined' | head -n 20 \
      | sed -E 's/^.*(undefined)? *external +//; s/ \(from .*\)$//' \
      | awk '{print "    "$0}'
    if [ "$UNDEF_SYM" -gt 20 ]; then
      note "... truncated (showing 20 of $UNDEF_SYM undefined)"
    fi
  fi
else
  warn "nm not found"
fi

# ---------------------------------------------------------------------------
# 6. Optional disassembly (opt-in, capped)
# ---------------------------------------------------------------------------
section "DISASSEMBLY"
if [ "$DO_DISASM" -eq 1 ]; then
  if have otool; then
    note "capped to first $DISASM_LINES lines of otool -tV (__TEXT,__text)"
    printf '\n'
    otool $ARCH_FLAG -tV "$TARGET" 2>/dev/null | head -n "$DISASM_LINES" | sed 's/^/  /'
    TOTAL_DIS=$(otool $ARCH_FLAG -tV "$TARGET" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$TOTAL_DIS" -gt "$DISASM_LINES" ]; then
      note "... truncated ($TOTAL_DIS total lines, showing $DISASM_LINES)"
    fi
  else
    warn "otool not found"
  fi
else
  note "skipped (pass --disasm to include a capped instruction listing)"
fi

# ---------------------------------------------------------------------------
# 7. Optional strings (opt-in, capped)
# ---------------------------------------------------------------------------
section "STRINGS"
if [ "$DO_STRINGS" -eq 1 ]; then
  warn "string output may include internal paths/identifiers; reviewing is your call"
  printf '\n'
  # strings(1) gives the human-readable printable view; `otool -s` would only
  # emit a hex dump. Fall back to otool's hex if strings is unavailable.
  if have strings; then
    # Default (no -a) keeps to __TEXT and avoids code-section noise.
    strings "$TARGET" 2>/dev/null | head -n "$STRING_LINES" | sed 's/^/  /'
  elif have otool; then
    warn "'strings' not found; showing raw (hex) __TEXT,__cstring section"
    otool $ARCH_FLAG -s __TEXT __cstring "$TARGET" 2>/dev/null \
      | head -n "$STRING_LINES" | sed 's/^/  /'
  else
    note "no strings mechanism available"
  fi
  note "capped to $STRING_LINES lines"
else
  note "skipped (no string dump by default; pass --strings to include one, capped)"
fi

# ---------------------------------------------------------------------------
# Wrap-up
# ---------------------------------------------------------------------------
section "SUMMARY"
printf '  inspection complete for: %s\n' "$TARGET"
printf '  mode: READ-ONLY, offline, non-mutating\n'
exit 0
