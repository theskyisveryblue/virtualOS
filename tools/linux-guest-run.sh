#!/usr/bin/env bash
# Boot a real Linux guest on Virtualization.framework with an interactive serial
# console, driven over a FIFO.
#
#   tools/linux-guest-run.sh --kernel /path/Image [--initrd /path/initramfs] \
#                            [--disk /path/rootfs.img] [--cmdline "..."] [--nested]
#
# The guest console is written to stdout; commands written to $VOS_LINUX_IN
# (default /private/tmp/vos-linux-in) are sent to the guest as console input.
#
# Artifact note: --kernel must be an *uncompressed* Linux Image. If you have a
# distro vmlinuz (compressed EFI-stub PE), extract the raw Image first:
#   tools/linux-extract-image.sh /path/vmlinuz /path/Image
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/tools/fleet/linux-guest"
FIFO="${VOS_LINUX_IN:-/private/tmp/vos-linux-in}"

if [[ ! -x "$BIN" ]]; then
  echo "building linux-guest..." >&2
  swiftc -framework Virtualization -framework Foundation "$ROOT/tools/fleet/LinuxGuest.swift" -o "$BIN"
fi

[[ $# -ge 1 ]] || { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

# Validate the kernel before creating the console FIFO.
kernel=""; prev=""
for a in "$@"; do
  [[ "$prev" == "--kernel" ]] && kernel="$a"
  prev="$a"
done
[[ -n "$kernel" ]] || { echo "missing --kernel" >&2; exit 2; }
[[ -e "$kernel" ]] || { echo "kernel not found: $kernel" >&2; exit 1; }

rm -f "$FIFO"; mkfifo -m 600 "$FIFO"
# An open, silent writer so the guest never sees EOF on its console input.
tail -f /dev/null > "$FIFO" &
writer=$!
trap 'kill "$writer" 2>/dev/null || true; rm -f "$FIFO"' EXIT

# Run the guest in the foreground (needs a GUI/Aqua session for the
# com.apple.security.virtualization entitlement; run via a LaunchAgent if
# launched from a plain SSH session).
exec "$BIN" "$@" < "$FIFO"
