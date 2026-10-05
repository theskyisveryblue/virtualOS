# Linux guests on Virtualization.framework (experimental / untested-boot)

> **Status: EXPERIMENTAL, NOT YET BOOTED.** No real Linux kernel or rootfs was
> used while building this helper. `--check` compiles and runs; `run` has never
> been executed against genuine artifacts in this repo. Treat any boot attempt
> as unverified.

Theme: **"Linux, not only macOS."** The shipping `virtualOS.app` boots only
macOS guests. This directory adds the Linux side of the story in a way that is
additive and does not touch the existing macOS path.

## Pieces

| File | Role |
| --- | --- |
| `tools/fleet/LinuxGuest.swift` | standalone Swift helper: builds a `VZLinuxBootLoader` VM |
| `tools/linux-guest.sh` | wrapper: `build` / `check` / `run` / `fetch-config` |
| `tools/LINUX_GUEST.md` | this document |

## What Apple's `VZLinuxBootLoader` needs

`VZLinuxBootLoader` boots a kernel directly — there is no firmware, no
`HardwareModel`, no `MachineIdentifier`, and no `AuxiliaryStorage` (those are all
macOS-guest concepts). It needs:

- **`kernelURL`** (required) — a local Linux kernel image (`vmlinuz`/`Image`),
  uncompressed. This is the raw kernel, not a distro package.
- **`initialRamdiskURL`** (optional) — an initramfs/initrd image.
- **`commandLine`** — kernel parameters. For a headless virtio guest the serial
  console shows up as `hvc0`, so a typical line is:
  `console=hvc0 root=/dev/vda rw`.

A `VZLinuxBootLoader` is **only valid with a `VZGenericPlatformConfiguration`**
(Apple's header states this explicitly). Using it with a Mac platform
configuration is invalid.

The full guest configuration used here also includes:

- `VZVirtioBlockDeviceConfiguration` + `VZDiskImageStorageDeviceAttachment` for
  the rootfs (exposed as `/dev/vda`). **Only RAW disk images are supported.**
- `VZVirtioNetworkDeviceConfiguration` with `VZNATNetworkDeviceAttachment` for
  host-NAT networking.
- `VZVirtioConsoleDeviceConfiguration` with a single port (`isConsole = true`)
  attached to stdout via `VZFileHandleSerialPortAttachment`.
- `VZVirtioEntropyDeviceConfiguration`.
- Rosetta directory sharing is **intentionally omitted** — it is a
  macOS-guest-only feature and is not needed for bare Linux.

## macOS vs Linux boot paths

| Concern | macOS guest | Linux guest |
| --- | --- | --- |
| Boot loader | `VZMacOSBootLoader` | `VZLinuxBootLoader` |
| Platform | `VZMacPlatformConfiguration` | `VZGenericPlatformConfiguration` |
| Firmware/boot chain | EFI + `HardwareModel` + `MachineIdentifier` + `AuxiliaryStorage` | none; kernel booted directly |
| Install flow | `VZMacOSInstaller` + restore image | supply kernel/initrd/rootfs yourself |
| Console | Mac graphics device (`VZMacGraphicsDeviceConfiguration`) | serial console (`VZVirtioConsoleDeviceConfiguration`) as `hvc0` |
| Disk layout | APFS/GUID `Disk.img` | arbitrary RAW image, root fs as `/dev/vda` |
| Rosetta | optional macOS-guest feature | not applicable |

Both share the same virtio storage/network/entropy devices and the same
`VZVirtualMachine` start/stop lifecycle.

## Obtaining a kernel + rootfs (high level)

No downloads happen in this repo. You supply the artifacts. Common sources:

1. **Apple's sample code** — the "Running Linux in a Virtual Machine" sample
   project ships a ready-made kernel/initrd and scripts to build a rootfs image.
   This is the closest match to this helper's configuration.
2. **Distro cloud images** — Debian, Ubuntu, Fedora, and Alpine all publish
   kernel + initramfs + disk images. Cloud images are often `qcow2`; convert to
   RAW with `qemu-img convert -O raw` first, because only RAW disk images are
   accepted.
3. **Alpine** — a minimal tiny kernel + initramfs is a common choice for a
   serial-only headless guest and keeps the boot loop fast to iterate.

Place the artifacts somewhere local and pass their paths explicitly. No artifact
paths are hardcoded or assumed to exist.

## Usage

```sh
# compile the helper -> tools/fleet/linux-guest
tools/linux-guest.sh build

# validate config/paths only; prints what WOULD boot (no VM starts)
tools/linux-guest.sh check \
    --kernel /path/to/vmlinuz \
    --initrd /path/to/initrd.img \
    --disk   /path/to/rootfs.raw \
    --cmdline "console=hvc0 root=/dev/vda rw"

# print the exact VZLinuxBootLoader usage + required artifacts (offline)
tools/linux-guest.sh fetch-config

# boot for real (blocks; console streams to stdout)
tools/linux-guest.sh run \
    --kernel /path/to/vmlinuz \
    --disk   /path/to/rootfs.raw
```

`check` validates that every referenced path exists and that CPU/memory are
sane, then prints the planned configuration without constructing or starting a
VM. `run` refuses to proceed if the kernel (or the disk, when given) is missing,
and prints a loud warning before booting. Ctrl-C stops the process.

Direct compilation, matching the helper's documented build line:

```sh
swiftc -framework Virtualization -framework Foundation \
    tools/fleet/LinuxGuest.swift -o tools/fleet/linux-guest
```

## What has and has not been verified

- **Verified:** `swiftc` compiles `LinuxGuest.swift` without a kernel present;
  `--check` fails gracefully on a nonexistent path; `--check` prints the planned
  configuration for paths that exist; `fetch-config` prints offline.
- **Not verified:** an actual Linux boot. No kernel, initrd, or rootfs was
  obtained or run during the construction of this helper. A real boot may
  require cmdline/console tuning (e.g. `console=hvc0` vs `ttyS0`, `root=`
  device naming) that can only be confirmed against real artifacts.
