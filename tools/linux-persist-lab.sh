#!/usr/bin/env bash
# Linux persistence laboratory: turn the throwaway boot-only Linux guest into a
# guest that has a root filesystem on its own disk and keeps a file across a
# reboot.
#
#   tools/linux-persist-lab.sh
#
# Why this exists: the Linux guest boots to a shell, but it boots from the
# initramfs and never touches its disk, so nothing it does survives. This lab
# formats the attached disk, lays down a minimal Alpine rootfs on it, boots FROM
# that disk with a real init, writes a marker file, reboots, and checks the
# marker is still there. It makes "persistent Linux machine" a measurement
# instead of a hope.
#
# Requirements, all local; nothing is downloaded by this script itself (the guest
# may fetch apk packages over its NAT interface, same as nested-lab.sh):
#   - an uncompressed Linux kernel Image + initramfs (see LINUX_GUEST.md)
#   - a RAW disk image (default: linux/inner-root.img); it is OVERWRITTEN
# Run on macOS in a GUI/Aqua session (the virtualization entitlement needs it).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLEET_DIR="$ROOT/tools/fleet"
LINUX_BIN="$FLEET_DIR/linux-guest"

ART="${VOS_LINUX_ART:-$ROOT/tools/nested-artifacts}"
FLEET_LINUX="${VOS_LINUX_HOME:-$HOME/vos-fleet/linux}"
KERNEL="${VOS_LINUX_KERNEL:-}"
INITRD="${VOS_LINUX_INITRD:-}"
DISK="${VOS_LINUX_DISK:-}"
OUT="${VOS_LINUX_OUT:-/private/tmp/vos-linux-persist}"
FIFO="/private/tmp/vos-linux-persist-in"
MARKER="/persist-marker.txt"

# Resolve artifacts: prefer explicit env, then the fleet linux dir, then the lab
# artifact cache.
[[ -n "$KERNEL" ]] || for c in "$FLEET_LINUX/Image" "$ART/Image"; do [[ -e "$c" ]] && { KERNEL="$c"; break; }; done
[[ -n "$INITRD" ]] || for c in "$FLEET_LINUX/initramfs" "$ART/initramfs"; do [[ -e "$c" ]] && { INITRD="$c"; break; }; done
[[ -n "$DISK"   ]] || DISK="$FLEET_LINUX/inner-root.img"

mkdir -p "$OUT"
echo "== linux-persist-lab: prerequisites =="
echo "   kernel: $KERNEL"
echo "   initrd: $INITRD"
echo "   disk:   $DISK  (will be reformatted)"
[[ -e "$KERNEL" ]] || { echo "need an uncompressed kernel Image (see tools/LINUX_GUEST.md)"; exit 1; }
[[ -e "$INITRD" ]] || { echo "need an initramfs"; exit 1; }
[[ -x "$LINUX_BIN" ]] || "$ROOT/tools/linux-guest.sh" build

# Work on a throwaway COW copy so the source disk is never the one we format.
WORK_DISK="$OUT/root.img"
rm -f "$WORK_DISK"
if [[ -e "$DISK" ]]; then cp -c "$DISK" "$WORK_DISK" 2>/dev/null || cp "$DISK" "$WORK_DISK";
else dd if=/dev/zero of="$WORK_DISK" bs=1m count=512 2>/dev/null; fi
echo "   work disk: $WORK_DISK ($(stat -f%z "$WORK_DISK") bytes)"

send() { printf '%s\n' "$1" > "$FIFO"; sleep "${2:-4}"; }

boot() {  # boot() <logfile> <cmdline> <waitfor>
  local log="$1" cmdline="$2" waitfor="$3"
  rm -f "$FIFO"; mkfifo -m 600 "$FIFO"
  tail -f /dev/null > "$FIFO" & WRITER=$!
  "$LINUX_BIN" --kernel "$KERNEL" --initrd "$INITRD" --disk "$WORK_DISK" \
    --cmdline "$cmdline" --cpu 4 --memory 3 < "$FIFO" > "$log" 2>&1 &
  GUEST=$!
  for _ in $(seq 1 90); do grep -qa "$waitfor" "$log" 2>/dev/null && return 0; sleep 1; done
  return 1
}
stop_guest() { kill "${GUEST:-}" 2>/dev/null || true; kill "${WRITER:-}" 2>/dev/null || true; rm -f "$FIFO"; }
trap 'stop_guest' EXIT

echo "== 1. boot install guest (initramfs shell) =="
if ! boot "$OUT/install-console.log" "console=hvc0" "emergency recovery shell"; then
  echo "install guest never reached a shell; see $OUT/install-console.log" >&2
  tail -20 "$OUT/install-console.log" >&2 || true
  exit 1
fi

echo "== 2. network + tools inside the guest =="
send "ip link set lo up; ip link set eth0 up; ip addr add 192.168.64.50/24 dev eth0 2>/dev/null; ip route add default via 192.168.64.1 2>/dev/null; echo nameserver 1.1.1.1 > /etc/resolv.conf; echo NET_DONE" 5
send "mkdir -p /lib/apk/db /var/cache/apk; printf 'https://dl-cdn.alpinelinux.org/alpine/v3.20/main\nhttps://dl-cdn.alpinelinux.org/alpine/v3.20/community\n' > /etc/apk/repositories; apk update >/dev/null 2>&1; echo APK_DONE" 20
# mkfs.ext4 is not in the initramfs busybox; pull e2fsprogs (and modules) first.
send "apk add --no-cache e2fsprogs e2fsprogs-extra >/dev/null 2>&1; echo E2FS_DONE \$(command -v mkfs.ext4)" 60

echo "== 3. format the disk (root=/dev/vda is the whole rootfs) =="
send "echo FORMAT_START; mkfs.ext4 -F -q /dev/vda && echo FORMAT_DONE || echo FORMAT_FAIL" 20

echo "== 4. lay down a minimal root fs with a real init =="
send "mount /dev/vda /mnt; mkdir -p /mnt/bin /mnt/sbin /mnt/etc /mnt/proc /mnt/sys /mnt/dev /mnt/root; echo ROOTDIR_DONE" 6
# apk --root installs a base Alpine rootfs into /mnt without touching the running guest.
send "apk --root /mnt --initdb --no-cache add alpine-base openrc busybox busybox-binsh >/dev/null 2>&1; echo ROOTFS_DONE $(test -x /mnt/bin/busybox && echo have_busybox)" 90
send "printf '%s\n' 'nameserver 1.1.1.1' > /mnt/etc/resolv.conf; printf 'root::0:0:root:/root:/bin/sh\n' > /mnt/etc/passwd; printf 'root:x:0:\n' > /mnt/etc/group; echo ETC_DONE" 6
# A tiny init that proves persistence: append the boot time to a marker on the disk.
cat > "$OUT/init.sh" <<'INIT'
#!/bin/sh
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
echo "init: boot #$(wc -l < /persist-marker.txt 2>/dev/null || echo 0) at $(cat /proc/uptime 2>/dev/null | cut -d' ' -f1)" >> /persist-marker.txt
echo "PERSIST":$(cat /persist-marker.txt 2>/dev/null | wc -l)
echo "init-ran"
exec /bin/sh
INIT
# push the init script into the mounted rootfs via the console (base64 to survive the shell)
B64=$(base64 < "$OUT/init.sh" | tr -d '\n')
send "printf '%s' '$B64' | base64 -d > /mnt/init; chmod +x /mnt/init; echo INIT_DONE" 6
send "umount /mnt && sync && echo UMOUNT_DONE" 6
send "echo INSTALL_COMPLETE" 2
stop_guest

echo "== 5. reboot FROM the disk (root=/dev/vda) =="
ROOT_CMDLINE="console=hvc0 root=/dev/vda rw rootfstype=ext4 modules=ext4 init=/init"
if ! boot "$OUT/reboot1-console.log" "$ROOT_CMDLINE" "init-ran"; then
  echo "disk-rooted boot did not reach init; see $OUT/reboot1-console.log" >&2
  tail -30 "$OUT/reboot1-console.log" >&2 || true
fi
send "cat /persist-marker.txt" 2
send "echo REBOOT1_COMPLETE" 2
stop_guest

echo "== 6. reboot again and confirm the marker grew =="
if ! boot "$OUT/reboot2-console.log" "$ROOT_CMDLINE" "init-ran"; then
  echo "second disk-rooted boot did not reach init" >&2
fi
send "cat /persist-marker.txt" 2
send "echo REBOOT2_COMPLETE" 2
stop_guest

echo "== linux-persist-lab result =="
python3 - "$OUT" "$MARKER" <<'PY'
import re, sys, json, pathlib
out = pathlib.Path(sys.argv[1])
def text(name):
    p = out / name
    if not p.exists(): return ""
    return p.read_bytes().decode("utf-8", "replace").replace("\x00", "").replace("\r", "\n")
install = text("install-console.log")
r1 = text("reboot1-console.log")
r2 = text("reboot2-console.log")
def marker_lines(raw):
    # lines the guest echoed from /persist-marker.txt look like "init: boot #N ..."
    return re.findall(r"init: boot #[0-9]+[^\n]*", raw)
m1, m2 = marker_lines(r1), marker_lines(r2)
print(json.dumps({
    "formatted": "FORMAT_DONE" in install,
    "rootfs_laid_down": "ROOTFS_DONE" in install,
    "init_installed": "INIT_DONE" in install,
    "disk_rooted_boot_1": "init-ran" in r1,
    "disk_rooted_boot_2": "init-ran" in r2,
    "marker_boot_1": m1[-1] if m1 else "",
    "marker_boot_2": m2[-1] if m2 else "",
    "persistent": bool(m1) and bool(m2) and m1[-1] != m2[-1],
    "verdict": ("persistent Linux rootfs: a file survived two reboots"
                if bool(m1) and bool(m2) and m1[-1] != m2[-1]
                else ("rootfs written but persistence not yet proven"
                      if "INIT_DONE" in install else "did not lay down a rootfs")),
}, indent=2, sort_keys=True))
PY
echo "artifacts under $OUT"
