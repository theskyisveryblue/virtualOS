# Linux guests on Virtualization.framework

> **Status: BOOTS a real Linux guest (verified).** An Alpine Linux aarch64
> kernel + initramfs was booted on Virtualization.framework: the kernel hands off
> to Alpine Init and the guest reaches an interactive emergency shell over the
> serial console. Driving commands over that console returned
> `Linux (none) 6.6.134-0-virt #1-Alpine SMP PREEMPT_DYNAMIC ... aarch64`.
>
> **Key gotcha:** `VZLinuxBootLoader` needs an **uncompressed kernel `Image`**. A
> distro `vmlinuz` is a compressed EFI-stub PE; if you pass it directly the VM
> starts but prints nothing. Extract first with
> `tools/linux-extract-image.sh vmlinuz Image`.

Theme: **"Linux, not only macOS."** The shipping `virtualOS.app` boots only
macOS guests. This directory adds the Linux side of the story in a way that is
additive and does not touch the existing macOS path.

## Pieces

| File | Role |
| --- | --- |
| `tools/fleet/LinuxGuest.swift` | standalone Swift helper: builds a `VZLinuxBootLoader` VM |
| `tools/linux-guest.sh` | wrapper: `build` / `check` / `run` / `fetch-config` / `selection-check` |
| `tools/linux_guest_selection.py` | read-only validation of an explicit future maintained or legacy selection |
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

## Prepare a maintained selection without activating it

The reviewed maintained pair is Alpine 3.24.2 with kernel 6.18.57. Its separate
two-boot receipt is
[`experiments/linux-maintained-persistence-receipt-20261018.json`](experiments/linux-maintained-persistence-receipt-20261018.json).
The existing Mini LaunchAgent still invokes `linux/run-guest.sh` with the
original `linux/Image`, `linux/initramfs`, and `linux/inner-root.img`. Nothing in
`selection-check` edits those paths, starts a VM, or changes that LaunchAgent.

[`experiments/linux-maintained-selection.example.json`](experiments/linux-maintained-selection.example.json)
contains the real retained profile/proof paths and placeholders for a future
private selection directory. It is a template, not an active configuration.
The artifact profile's `boot_verified: false` describes its original build;
the separately pinned persistence receipt supplies the actual two-boot evidence.

Prepare a runtime disk only during a later healthy resource window:

1. Run `python3 tools/linux_lab_resources.py check` and require success. Confirm
   the retained proof disk has no open descriptors. Keep the current Linux
   guest, source disk, and original boot artifacts unchanged.
2. Create a **new**, owned mode-0700 directory for the runtime copy. Refuse an
   existing destination directory or linked path. Use APFS `cp -c` to copy the
   retained proof disk into that new directory as `root.img`, then set mode
   0600. Never use the proof disk itself as a writable runtime disk. A copy
   failure should stop preparation; do not fall back to a full copy under
   storage pressure.
3. Copy the reviewed receipt and example JSON into the private directory, with
   mode 0600. Replace both `__PRIVATE_SELECTION_DIRECTORY__` placeholders with
   its literal absolute path. Keep the receipt SHA-256 and maintained artifact
   pins unchanged; the selector checks the actual receipt, kernel, initrd,
   proof disk, and separate runtime disk. It hashes disks in bounded 1 MiB reads.
4. Validate the private configuration:

   ```sh
   tools/linux-guest.sh selection-check /absolute/private/directory/selection.json
   ```

Success returns proposed arguments and `activation_required: true`. It proves
only the **initial** prepared copy: once that runtime disk has guest writes,
this initial-copy check deliberately refuses it. Persistent runtime identity
tracking, launch integration, resource admission at activation, and rollout
remain separate work. A failed maintained check never selects legacy artifacts;
legacy validation requires an explicit `mode: "legacy"` configuration with
each artifact's path and SHA-256.

## What has and has not been verified

Failed private persistence images can be made sparse through
`linux_lab_storage.sparsify` after an explicit SHA-256 review and unused-file
checks. It builds and rereads a sparse copy before atomic replacement, preserves
every logical byte and diagnostic log, and refuses successful proof images.
On October 20, one failed, entirely zero 256 MiB image went from 256 MiB of
reported allocation to zero while retaining its exact SHA-256. The source image
remained open and was preserved. Per-file allocation is not a measurement of
unique APFS extents or of net volume space gained; see
`experiments/failed-linux-sparse-receipt-20261020.json`.

- **Verified:** `swiftc` compiles `LinuxGuest.swift` without a kernel present;
  `--check` fails gracefully on a nonexistent path; `--check` prints the planned
  configuration for paths that exist; `fetch-config` prints offline.
- **Verified:** real Alpine boot and, separately, two disk-rooted persistence
  boots of both the legacy Alpine 3.20 baseline and maintained Alpine 3.24.2.
  Both persistence proofs locked root password login, retained their private
  proof disks/logs, and left existing guests running. See the two October 18
  persistence receipts under `experiments/` for pins and scope.
- **Open:** activation of the maintained profile as the default Linux guest,
  runtime identity tracking after initial writes, and nested virtualization.
