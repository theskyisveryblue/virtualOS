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
#   - an optional RAW disk image (default: linux/inner-root.img); only a private
#     disposable copy is formatted
#   - an initramfs with ext4 support matching the kernel
# Run on macOS in a GUI/Aqua session (the virtualization entitlement needs it).
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLEET_DIR="$ROOT/tools/fleet"
LINUX_BIN="$FLEET_DIR/linux-guest"

ART="${VOS_LINUX_ART:-$ROOT/tools/nested-artifacts}"
FLEET_LINUX="${VOS_LINUX_HOME:-$HOME/vos-fleet/linux}"
KERNEL="${VOS_LINUX_KERNEL:-}"
INITRD="${VOS_LINUX_INITRD:-}"
DISK="${VOS_LINUX_DISK:-}"
OUT_PREFIX="${VOS_LINUX_OUT:-$FLEET_LINUX/persistence-tests/run}"
MARKER="/persist-marker.txt"
MAINTAINED_PROFILE="${VOS_LINUX_MAINTAINED_PROFILE:-}"
if [[ -n "$MAINTAINED_PROFILE" ]]; then
  python3 "$ROOT/tools/linux_maintained_profile.py" check "$MAINTAINED_PROFILE"
  KERNEL="$(dirname "$MAINTAINED_PROFILE")/Image"
  INITRD="$(dirname "$MAINTAINED_PROFILE")/initramfs"
fi

# Resolve artifacts: prefer explicit env, then the fleet linux dir, then the lab
# artifact cache.
[[ -n "$KERNEL" ]] || for c in "$FLEET_LINUX/Image" "$ART/Image"; do [[ -e "$c" ]] && { KERNEL="$c"; break; }; done
[[ -n "$INITRD" ]] || for c in "$FLEET_LINUX/initramfs" "$ART/initramfs"; do [[ -e "$c" ]] && { INITRD="$c"; break; }; done
[[ -n "$DISK"   ]] || DISK="$FLEET_LINUX/inner-root.img"

echo "== linux-persist-lab: prerequisites =="
echo "   kernel: $KERNEL"
echo "   initrd: $INITRD"
echo "   disk:   $DISK  (source for a disposable copy)"
[[ -e "$KERNEL" ]] || { echo "need an uncompressed kernel Image (see tools/LINUX_GUEST.md)"; exit 1; }
[[ -e "$INITRD" ]] || { echo "need an initramfs"; exit 1; }
python3 "$ROOT/tools/linux_boot_artifacts.py" "$KERNEL" "$INITRD"
# This lab fixes its guest budget to 1 CPU / 1 GiB, admits only with fresh
# headroom, and monitors pressure throughout. Larger VM/model gates stay strict.
python3 "$ROOT/tools/linux_lab_resources.py" check || exit 75
if [[ -z "${VOS_LINUX_OUT:-}" ]]; then
  python3 "$ROOT/tools/linux_lab_resources.py" prepare-output "$FLEET_LINUX/persistence-tests"
fi
OUT="$(mktemp -d "${OUT_PREFIX}.XXXXXXXX")"
FIFO="$OUT/console-in"
[[ -x "$LINUX_BIN" ]] || "$ROOT/tools/linux-guest.sh" build

# Work on a throwaway COW copy so the source disk is never the one we format.
WORK_DISK="$OUT/root.img"
python3 "$ROOT/tools/linux_lab_storage.py" initialize "$OUT" "$DISK" "$FLEET_LINUX/persistence-tests"

prepare_work_disk() {
  [[ ! -e "$WORK_DISK" ]] || return 0
  if [[ -e "$DISK" ]]; then cp -c "$DISK" "$WORK_DISK" 2>/dev/null || cp "$DISK" "$WORK_DISK" || return 1;
  else dd if=/dev/zero of="$WORK_DISK" bs=1m count=512 2>/dev/null || return 1; fi
  chmod 600 "$WORK_DISK" || return 1
  echo "   work disk: $WORK_DISK ($(stat -f%z "$WORK_DISK") bytes)"
}

wait_marker() {
  local marker="$1" timeout="$2"
  for ((attempt=0; attempt<timeout; attempt++)); do
    [[ ! -e "$OUT/resource-abort" ]] && kill -0 "$RESOURCE_WATCH" 2>/dev/null || {
      echo 'linux lab stopped: resource monitor refused continuation' >&2; exit 75;
    }
    if LC_ALL=C tr -d '\r' < "$CURRENT_LOG" | grep -aqx "$marker"; then return 0; fi
    kill -0 "$GUEST" 2>/dev/null || break
    sleep 1
  done
  echo "guest did not confirm $marker; see $CURRENT_LOG" >&2
  return 1
}

send() { # send <shell command> <success marker> [timeout]
  # Encoding prevents the console's command echo from satisfying a marker.
  local payload
  payload="$(printf '%s\n' "set -e" "$1" "printf '\\n%s\\n' '$2'" | base64 | tr -d '\n')"
  python3 "$FLEET_DIR/../linux_lab_console.py" "$FIFO" "$GUEST" "$payload"
  wait_marker "$2" "${3:-30}"
}

boot() {  # boot() <logfile> <cmdline> <waitfor>
  local log="$1" cmdline="$2" waitfor="$3"
  python3 "$ROOT/tools/linux_lab_resources.py" check || exit 75
  prepare_work_disk || return 1
  CURRENT_LOG="$log"
  rm -f "$OUT/resource-abort"
  rm -f "$FIFO"; mkfifo -m 600 "$FIFO"
  tail -f /dev/null > "$FIFO" & WRITER=$!
  "$LINUX_BIN" --kernel "$KERNEL" --initrd "$INITRD" --disk "$WORK_DISK" \
    --cmdline "$cmdline" --cpu 1 --memory 1 < "$FIFO" > "$log" 2>&1 &
  GUEST=$!
  python3 "$ROOT/tools/linux_lab_resources.py" watch "$GUEST" "$OUT/resource-abort" \
    >> "$OUT/resource-watch.log" 2>&1 &
  RESOURCE_WATCH=$!
  for _ in $(seq 1 90); do
    [[ ! -e "$OUT/resource-abort" ]] && kill -0 "$RESOURCE_WATCH" 2>/dev/null || exit 75
    grep -qa "$waitfor" "$log" 2>/dev/null && return 0
    kill -0 "$GUEST" 2>/dev/null || return 1
    sleep 1
  done
  return 1
}
stop_guest() {
  kill "${RESOURCE_WATCH:-}" 2>/dev/null || true
  kill "${GUEST:-}" 2>/dev/null || true
  kill "${WRITER:-}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "${GUEST:-}" 2>/dev/null || break
    sleep 0.25
  done
  kill -KILL "${GUEST:-}" 2>/dev/null || true
  wait "${GUEST:-}" 2>/dev/null || true
  wait "${WRITER:-}" 2>/dev/null || true
  wait "${RESOURCE_WATCH:-}" 2>/dev/null || true
  GUEST=""; WRITER=""; RESOURCE_WATCH=""
  rm -f "$FIFO"
}
finish() {
  local status=$?
  trap - EXIT
  stop_guest
  if ! python3 "$ROOT/tools/linux_lab_storage.py" finalize "$OUT" "$status" "$FLEET_LINUX/persistence-tests"; then
    # An unproven cleanup must not trigger a repeated resource-pressure loop.
    status=1
  fi
  exit "$status"
}
trap finish EXIT

echo "== 1. boot install guest (initramfs shell) =="
if ! boot "$OUT/install-console.log" "console=hvc0" "emergency recovery shell"; then
  echo "install guest never reached a shell; see $OUT/install-console.log" >&2
  tail -20 "$OUT/install-console.log" >&2 || true
  exit 1
fi

echo "== 2. network + tools inside the guest =="
send 'modprobe ext4; grep -qw ext4 /proc/filesystems' EXT4_READY 15
if [[ -n "$MAINTAINED_PROFILE" ]]; then
  send 'modprobe af_packet; grep -qw PACKET /proc/net/protocols' PACKET_READY 15
fi
send 'ip link set lo up; ip link set eth0 up; udhcpc -i eth0 -n -q -t 5' NET_DONE 45
if [[ -n "$MAINTAINED_PROFILE" ]]; then
  send 'test -f /etc/virtualos-lab-profile.json; grep -q "^3[.]24[.]" /etc/alpine-release; cd /var/cache/apk/packages; sha256sum -c SHA256SUMS; apk --version; command -v mkfs.ext4' APK_DONE 45
else
  send "mkdir -p /lib/apk/db /var/cache/apk; printf 'https://dl-cdn.alpinelinux.org/alpine/v3.20/main\nhttps://dl-cdn.alpinelinux.org/alpine/v3.20/community\n' > /etc/apk/repositories; apk --initdb add --no-cache e2fsprogs e2fsprogs-extra" APK_DONE 120
fi
# mkfs.ext4 is not in the initramfs busybox; pull e2fsprogs (and modules) first.
send 'command -v mkfs.ext4' E2FS_DONE 15

echo "== 3. format the disk (root=/dev/vda is the whole rootfs) =="
send 'mkfs.ext4 -F -q /dev/vda' FORMAT_DONE 60

echo "== 4. lay down a minimal root fs with a real init =="
send 'mkdir -p /mnt; mount /dev/vda /mnt; mkdir -p /mnt/bin /mnt/sbin /mnt/etc/apk/keys /mnt/proc /mnt/sys /mnt/dev /mnt/root; cp /etc/apk/keys/*.pub /mnt/etc/apk/keys/; cp /etc/apk/repositories /mnt/etc/apk/repositories' ROOTDIR_DONE
# apk --root installs a base Alpine rootfs into /mnt without touching the running guest.
if [[ -n "$MAINTAINED_PROFILE" ]]; then
  send 'apk --root /mnt --initdb --no-network add /var/cache/apk/packages/*.apk; cp -a /lib/modules /mnt/lib/; cp /etc/virtualos-lab-profile.json /mnt/etc/virtualos-lab-profile.json; test -x /mnt/bin/busybox; grep -q "^3[.]24[.]" /mnt/etc/alpine-release; printf "\nMAINTAINED_USERLAND\n"' ROOTFS_DONE 180
else
  send 'apk --root /mnt --initdb --no-cache add alpine-base openrc busybox busybox-binsh; test -x /mnt/bin/busybox' ROOTFS_DONE 180
fi
send "printf '%s\n' 'nameserver 1.1.1.1' > /mnt/etc/resolv.conf; printf 'root:x:0:0:root:/root:/bin/sh\n' > /mnt/etc/passwd; printf 'root:x:0:\n' > /mnt/etc/group; printf 'root:!:0:0:99999:7:::\n' > /mnt/etc/shadow; chmod 600 /mnt/etc/shadow; grep -qx 'root:!:0:0:99999:7:::' /mnt/etc/shadow; test \"\$(stat -c %a /mnt/etc/shadow)\" = 600; printf '\\nROOT_LOGIN_LOCKED\\n'" ETC_DONE
# A tiny init that proves persistence: append the boot time to a marker on the disk.
cat > "$OUT/init.sh" <<'INIT'
#!/bin/sh
set -e
mountpoint -q /proc || mount -t proc proc /proc
mountpoint -q /sys || mount -t sysfs sys /sys
mountpoint -q /dev || mount -t devtmpfs dev /dev
# Alpine's early init mounts the selected root read-only; normal init owns this
# remount. This private test init must do the same before recording its boot.
mount -o remount,rw /
awk '$2 == "/" && $3 == "ext4" && $4 ~ /(^|,)rw(,|$)/ { found=1 } END { exit !found }' /proc/mounts
previous=0
if [ -f /persist-marker.txt ]; then previous=$(wc -l < /persist-marker.txt); fi
echo "init: boot #$previous at $(cut -d' ' -f1 /proc/uptime)" >> /persist-marker.txt
sync
printf 'PERSIST:%s\n' "$(wc -l < /persist-marker.txt | tr -d ' ')"
printf 'ALPINE_RELEASE:%s\n' "$(cat /etc/alpine-release)"
echo "init-ran"
exec /bin/sh
INIT
# push the init script into the mounted rootfs via the console (base64 to survive the shell)
B64=$(base64 < "$OUT/init.sh" | tr -d '\n')
send "printf '%s' '$B64' | base64 -d > /mnt/init; chmod +x /mnt/init; test -x /mnt/init" INIT_DONE
send 'umount /mnt; sync' UMOUNT_DONE
stop_guest

echo "== 5. reboot FROM the disk (root=/dev/vda) =="
ROOT_CMDLINE="console=hvc0 root=/dev/vda rw rootfstype=ext4 modules=ext4 init=/init"
if ! boot "$OUT/reboot1-console.log" "$ROOT_CMDLINE" "init-ran"; then
  echo "disk-rooted boot did not reach init; see $OUT/reboot1-console.log" >&2
  tail -30 "$OUT/reboot1-console.log" >&2 || true
  exit 1
fi
send 'cat /persist-marker.txt' REBOOT1_COMPLETE
stop_guest

echo "== 6. reboot again and confirm the marker grew =="
if ! boot "$OUT/reboot2-console.log" "$ROOT_CMDLINE" "init-ran"; then
  echo "second disk-rooted boot did not reach init" >&2
  exit 1
fi
send 'cat /persist-marker.txt' REBOOT2_COMPLETE
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
root_locked = bool(re.search(r'^ROOT_LOGIN_LOCKED$', install, re.M))
maintained = ('MAINTAINED_USERLAND' in install and
              bool(re.search(r'^ALPINE_RELEASE:3\.24\.[0-9]+$', r1, re.M)) and
              bool(re.search(r'^ALPINE_RELEASE:3\.24\.[0-9]+$', r2, re.M)))
proved = root_locked and bool(re.search(r'^PERSIST:1$', r1, re.M)) and bool(re.search(r'^PERSIST:2$', r2, re.M))
if __import__('os').environ.get('VOS_LINUX_MAINTAINED_PROFILE'):
    proved = proved and maintained
print(json.dumps({
    "formatted": "FORMAT_DONE" in install,
    "rootfs_laid_down": "ROOTFS_DONE" in install,
    "init_installed": "INIT_DONE" in install,
    "root_login_locked": root_locked,
    "maintained_userland": maintained,
    "disk_rooted_boot_1": "init-ran" in r1,
    "disk_rooted_boot_2": "init-ran" in r2,
    "marker_boot_1": m1[-1] if m1 else "",
    "marker_boot_2": m2[-1] if m2 else "",
    "persistent": proved,
    "verdict": ("persistent Linux rootfs: a file survived two reboots"
                if proved
                else ("rootfs written but persistence not yet proven"
                      if "INIT_DONE" in install else "did not lay down a rootfs")),
}, indent=2, sort_keys=True))
sys.exit(0 if proved else 1)
PY
echo "artifacts under $OUT"
