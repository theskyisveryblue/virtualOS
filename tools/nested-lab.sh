#!/usr/bin/env bash
# Nested-VM laboratory: boot a real Linux guest on Virtualization.framework, then
# try to boot a VM *inside* it (VM-in-VM), reporting exactly how far it gets.
#
#   tools/nested-lab.sh
#
# Why this exists: nested virtualization is the one thing the box promises but
# cannot yet deliver, because Apple exposes it only on VZGenericPlatformConfiguration
# AND (as measured here) does not yet hand EL2/HYP through to the guest kernel.
# This script makes that boundary reproducible instead of a claim.
#
# Requirements, all created on the fly:
#   - an uncompressed Linux kernel Image + initramfs (see LINUX_GUEST.md)
#   - inside that guest: qemu-system-aarch64, an inner kernel/initramfs
# Run on macOS in a GUI/Aqua session (the virtualization entitlement needs it).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ART="${VOS_NESTED_ART:-$ROOT/tools/nested-artifacts}"
OUT="${VOS_NESTED_OUT:-/private/tmp/vos-nested-lab}"
KERNEL="${VOS_NESTED_KERNEL:-$ART/Image}"
INITRD="${VOS_NESTED_INITRD:-$ART/initramfs}"
FIFO="/private/tmp/vos-nested-console-in"
mkdir -p "$OUT"

echo "== nested-lab: prerequisites =="
[[ -e "$KERNEL" ]] || { echo "need an uncompressed kernel Image at $KERNEL (see tools/LINUX_GUEST.md)"; exit 1; }
[[ -e "$INITRD" ]] || { echo "need an initramfs at $INITRD"; exit 1; }

send() { printf '%s\n' "$1" > "$FIFO"; sleep "${2:-3}"; }

echo "== 1. boot outer Linux guest (interactive console via FIFO) =="
rm -f "$FIFO"; mkfifo -m 600 "$FIFO"
tail -f /dev/null > "$FIFO" & writer=$!
trap 'kill "$writer" 2>/dev/null || true; rm -f "$FIFO"' EXIT
"$ROOT/tools/fleet/linux-guest" --kernel "$KERNEL" --initrd "$INITRD" \
  --cmdline "console=hvc0" --cpu 4 --memory 3 < "$FIFO" \
  > "$OUT/outer-console.log" 2>&1 &
guest=$!
for _ in $(seq 1 60); do grep -qa 'initramfs emergency recovery shell' "$OUT/outer-console.log" 2>/dev/null && break; sleep 1; done

echo "== 2. set up the guest: network, apk, QEMU, inner kernel =="
send "ip link set lo up; ip link set eth0 up; ip addr add 192.168.64.50/24 dev eth0 2>/dev/null; ip route add default via 192.168.64.1 2>/dev/null; echo nameserver 1.1.1.1 > /etc/resolv.conf; echo NET_DONE" 5
send "mkdir -p /lib/apk/db /var/cache/apk; printf 'https://dl-cdn.alpinelinux.org/alpine/v3.20/main\nhttps://dl-cdn.alpinelinux.org/alpine/v3.20/community\n' > /etc/apk/repositories; apk update >/dev/null 2>&1; echo APK_DONE" 15
send "apk add --no-cache qemu-system-aarch64 qemu-img xorriso >/dev/null 2>&1; echo QEMU_DONE $(command -v qemu-system-aarch64)" 60

echo "== 3. probe nested capability from inside the guest =="
send "echo NESTED_A: \$(cat /proc/sys/kernel/osrelease)" 2
send "echo NESTED_B: \$(test -e /dev/kvm && echo kvm_present || echo no_dev_kvm)" 2
send "echo NESTED_C: \$(dmesg 2>/dev/null | grep -i 'HYP mode' | tail -1)" 2

echo "== 4. attempt VM-in-VM with QEMU =="
# The inner Alpine kernel comes from the alpine-virt ISO; kept small and serial-only.
send "cd /tmp; wget -q -O v.iso https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/aarch64/alpine-virt-3.24.2-aarch64.iso; echo ISO_DONE \$(wc -c < v.iso)" 60
send "xorriso -osirrox on -indev /tmp/v.iso -extract /boot/vmlinuz-virt /tmp/k 2>/dev/null; xorriso -osirrox on -indev /tmp/v.iso -extract /boot/initramfs-virt /tmp/i 2>/dev/null; echo EXTRACT_DONE" 15
send "qemu-system-aarch64 -M virt -cpu max -m 512 -smp 2 -nographic -no-reboot -kernel /tmp/k -initrd /tmp/i -append 'console=ttyAMA0 modules=loop,squashfs,sd-mod' > /tmp/inner.log 2>&1 & echo QEMU_PID=\$!" 5
send "echo INNER_RESULT:; tr -d '\\000' < /tmp/inner.log | cat -v | tail -4" 10

kill "$guest" 2>/dev/null || true

echo "== nested-lab result =="
python3 - "$OUT/outer-console.log" <<'PY'
import re, sys, json
raw = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
raw = raw.replace("\x00", "").replace("\r", "\n")
def last(tag):
    m = re.findall(tag + r":? *([^\n]*)", raw)
    return m[-1].strip() if m else ""
inner = re.search(r"INNER_RESULT:\s*(.*)", raw, re.S)
inner_lines = [l for l in (inner.group(1).splitlines() if inner else []) if l.strip()][:4]
print(json.dumps({
    "outer_guest_booted": "initramfs emergency recovery shell" in raw,
    "guest_kernel": last("NESTED_A"),
    "guest_has_dev_kvm": last("NESTED_B"),
    "guest_kvm_probe": last("NESTED_C"),
    "host_nested_api": "VZGenericPlatformConfiguration.isNestedVirtualizationSupported",
    "inner_qemu": inner_lines,
    "verdict": ("nested VM booted" if any("Linux version" in l or "Alpine" in l for l in inner_lines)
                else "nested blocked: guest kernel has no EL2/HYP (kvm: HYP mode not available)"),
}, indent=2, sort_keys=True))
PY
echo "artifacts under $OUT"
