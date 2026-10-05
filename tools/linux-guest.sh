#!/usr/bin/env bash
# virtualOS Linux guest wrapper (EXPERIMENTAL / untested-boot).
#
# Sibling to computer-fleet.sh: instead of macOS VM bundles, this drives a
# VZLinuxBootLoader-based generic Linux guest via tools/fleet/LinuxGuest.swift.
#
#   linux-guest.sh build
#   linux-guest.sh check --kernel PATH [--initrd PATH] [--disk PATH] [--cmdline "..."]
#   linux-guest.sh run   --kernel PATH [--initrd PATH] [--disk PATH] [--cmdline "..."]
#   linux-guest.sh fetch-config
#
# No network access and no artifact downloads happen anywhere in this script.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLEET_DIR="$ROOT/tools/fleet"
SRC="$FLEET_DIR/LinuxGuest.swift"
BIN="$FLEET_DIR/linux-guest"

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
}

build() {
  echo "building linux-guest..."
  swiftc -framework Virtualization -framework Foundation "$SRC" -o "$BIN"
  echo "built: $BIN"
}

ensure_bin() {
  [[ -x "$BIN" ]] || build
}

check() {
  ensure_bin
  "$BIN" --check "$@"
}

run() {
  # Refuse to boot unless the kernel path given on the command line exists.
  # Parse just enough to fail gracefully before the Swift helper warns/boots.
  local kernel=""
  local disk=""
  local prev=""
  for a in "$@"; do
    case "$prev" in
      --kernel) kernel="$a" ;;
      --disk) disk="$a" ;;
    esac
    prev="$a"
  done
  if [[ -z "$kernel" ]]; then
    echo "run: missing --kernel PATH" >&2
    exit 2
  fi
  if [[ ! -e "$kernel" ]]; then
    echo "run: kernel does not exist: $kernel" >&2
    exit 1
  fi
  if [[ -n "$disk" && ! -e "$disk" ]]; then
    echo "run: disk image does not exist: $disk" >&2
    exit 1
  fi
  ensure_bin
  cat >&2 <<'WARN'
########################################################################
# WARNING: REAL LINUX BOOT REQUESTED (EXPERIMENTAL / UNTESTED).
#
# This needs a genuine Linux kernel image, an optional initrd, and a rootfs
# disk image. virtualOS ships none of these. If any artifact is wrong the
# guest will fail to boot. See tools/LINUX_GUEST.md before proceeding.
########################################################################
WARN
  "$BIN" "$@"
}

fetch_config() {
  cat <<'DOC'
virtualOS Linux guest — configuration and required artifacts
============================================================
Nothing here downloads anything. This is a blueprint only.

What VZLinuxBootLoader needs
----------------------------
  * kernelURL           : a local Linux kernel image (raw vmlinuz/Image, not a
                          compressed package). REQUIRED.
  * initialRamdiskURL   : an optional initramfs/initrd image. Optional.
  * commandLine         : the kernel cmdline. For a headless virtio guest the
                          serial console is hvc0, so a typical value is:
                              console=hvc0 root=/dev/vda rw

  A VZLinuxBootLoader is only valid with a VZGenericPlatformConfiguration.

Artifacts this guest configuration expects
------------------------------------------
  1. kernel   (file)  e.g. vmlinuz / Image
  2. initrd   (file, optional)  e.g. initrd.img / initramfs-linux.img
  3. rootfs   (file)  a RAW disk image containing the root filesystem, exposed
                      to the guest as /dev/vda via VZVirtioBlockDeviceConfiguration.

Exact VZLinuxBootLoader usage (Swift)
-------------------------------------
  let bootLoader = VZLinuxBootLoader(kernelURL: kernelURL)
  bootLoader.commandLine = "console=hvc0 root=/dev/vda rw"
  bootLoader.initialRamdiskURL = initrdURL            // optional

  let platform = VZGenericPlatformConfiguration()

  let config = VZVirtualMachineConfiguration()
  config.platform = platform
  config.bootLoader = bootLoader
  config.cpuCount = 2
  config.memorySize = 2 * 1024 * 1024 * 1024

  let attachment = try VZDiskImageStorageDeviceAttachment(url: diskURL, readOnly: false)
  config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]

  let network = VZVirtioNetworkDeviceConfiguration()
  network.attachment = VZNATNetworkDeviceAttachment()
  config.networkDevices = [network]

  let console = VZVirtioConsoleDeviceConfiguration()
  let port = VZVirtioConsolePortConfiguration()
  port.isConsole = true
  port.attachment = VZFileHandleSerialPortAttachment(
      fileHandleForReading: FileHandle.standardInput,
      fileHandleForWriting: FileHandle.standardOutput)
  console.ports[0] = port
  config.consoleDevices = [console]

  config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
  try config.validate()

Where to get a kernel + rootfs (high level, no download here)
-------------------------------------------------------------
  * Apple's "Running Linux in a Virtual Machine" sample code project includes a
    ready-made Linux kernel/initrd and scripts that build a rootfs image.
  * Mainstream distro cloud images (Debian, Ubuntu, Fedora, Alpine) publish
    kernel + initramfs + a RAW/qcow2 disk image; convert qcow2 to RAW with
    qemu-img before use, since only RAW disk images are supported.
  * Alpine's virt ISO/tiny kernel is a common minimal choice for serial-only
    headless guests.

Notes
-----
  * Only RAW disk images are supported by VZDiskImageStorageDeviceAttachment.
  * Nested virtualization is unrelated here; the guest is a normal generic VM.
  * Rosetta directory sharing is macOS-guest-only and is intentionally omitted.
DOC
}

cmd="${1:-}"
shift || true
case "$cmd" in
  build) build ;;
  check) check "$@" ;;
  run) run "$@" ;;
  fetch-config) fetch_config ;;
  *) usage; exit 2 ;;
esac
