# `fs-inspect.sh` — read-only filesystem understanding toolkit

`tools/fs-inspect.sh` inspects a disk image file **or** a mounted
filesystem and reports what it is, how much of it is physically allocated,
and whether it is sparse — **without reading the whole file and without
writing anything**. It exists to make the manifesto theme *"Filesystems as
first-class citizens"* (MANIFESTO.md) concrete: machines are files, a disk
image is a structure to read, and storage is a system — a base, an overlay,
a checkpoint.

> The production VM disk is sacred. This tool never writes, never attaches,
> never mounts. It refuses to inspect the production disk or any `.bundle`
> path unless `--allow-production` is passed, and even then it stays strictly
> read-only.

## Purpose

- Make **RAW / sparse RAW**, **APFS containers and snapshots**, the
  **ASIF direction**, and **base + copy-on-write overlay** legible from the
  command line.
- Give a safe, reproducible way to answer "how big is this image, really?"
  when the manifesto insists a machine must be a file.
- Never disturb a live VM. This is an *inspection* tool, labelled as such.

## Usage

```sh
./tools/fs-inspect.sh [--allow-production] [--no-hash] <path> [overlay ...]
```

| Argument | Meaning |
| --- | --- |
| `<path>` | A disk image file **or** a mounted filesystem path / mount point. |
| `[overlay ...]` | Optional overlay or checkpoint files to report conceptually. |
| `--allow-production` | Opt in to inspecting a path inside a `.bundle` or the production disk. Still read-only. |
| `--no-hash` | Skip the capped SHA-256 of overlay files. |
| `-h`, `--help` | Print usage. |

Exit codes: `0` success, `1` missing path, `2` bad usage, `3` safety refusal.

## What it reports

### 1. File-level facts (for a file)

- `ls -l@` — mode, size, extended attributes.
- `stat -f '%z %b %k'` — logical size, 512-byte block count, optimal I/O.
- **Sparse detection** — compares logical size against `blocks * 512`. If
  allocated < logical, it is sparse, and the report shows the unallocated
  bytes and percentage. It reads no file contents for this.
- `xattr -l` — lists extended attributes and flags the APFS compression
  attributes `com.apple.decmpfs` and `com.apple.ResourceFork` when present.
- `file` — one-line type from the header.

### 2. Image type detection

- `file <path>` for a first-pass descriptor.
- `diskutil image info <path>` when available — **read-only**, and failures
  are tolerated.
- A **heuristic verdict**: APFS (file(1) recognition or `NXSB` container
  magic), ASIF (Apple sparse image / macOS 26 direction), RAW with GPT
  (`EFI PART`), or plain RAW. At most a two-sector header is sampled, never
  the full file.

### 3. `hdiutil imageinfo`

`hdiutil imageinfo <path>` is read-only and never attaches. The script
prints the class, format, and format description, then a partition summary
when the image carries one. Unsupported formats fail gracefully.

### 4. Mounted path facts

For a directory or mount point:

- `df -h <path>`
- `mount | grep` on the resolved mount point
- `diskutil info <dev-or-mount>` — the script resolves the backing device
  from `df` because `diskutil info` accepts a device node or mount point,
  then adds a mount-point view.
- `diskutil apfs list`, **filtered** to the relevant APFS container (found
  via the volume's `APFS Container:` reference), so snapshots, physical
  stores, capacity, and per-volume usage are shown without the whole table.

### 5. Base + overlay / checkpoint concept

The manifesto model is a verified read-only base plus a copy-on-write
overlay or checkpoint: the base never changes, only the overlay records new
writes, so one image can serve many machines. Given extra overlay paths, the
script reports each one's size, allocated bytes, sparseness, and a SHA-256
**capped at the first 64 MiB** (a full hash of a 30 GiB image would be a
read; this tool avoids that by default).

### 6. Safety

- Resolves the target and compares it to `~/VirtualOSMachines/virtualOS.bundle/Disk.img`.
- Refuses, with exit code `3`, if the target is that disk, is inside any
  `.bundle`, or is inside the production bundle directory — unless
  `--allow-production` is set.
- Even with `--allow-production`, the tool performs no writes, no write
  attach, and no mount.

## Concepts

### RAW and sparse RAW

A RAW image is a direct byte-for-byte disk. A **sparse RAW** has logical
size larger than its physical allocation: unwritten ranges are holes.
macOS reports this through `st_blocks`; the script compares
`st_size` to `st_blocks * 512` to prove sparseness without scanning data.
The README notes the Mini's production RAW was reported as 30 GiB logical
and about 25.6 GiB allocated — already roughly 4.45 GiB sparse — and
`SEEK_HOLE`/`SEEK_DATA` can locate holes without reading bytes. This tool
reports the aggregate; it does not map individual holes.

### APFS containers and snapshots

APFS groups one or more **volumes** under a **container** (an APFS Physical
Store, often a partition). A container has several volumes (System, Data,
Preboot, Recovery, VM) that share free space, and each volume can carry
**snapshots** — read-only point-in-time copies used by `tmutil` and the
sealed system volume. `diskutil apfs list` shows containers, volumes,
snapshots, and capacity. The script filters this to the container backing
your target. This matters because a claimed "free space" is the container's,
not a single volume's, and because APFS shares copy-on-write extents: adding
file sizes across a base and its clone overstates physical usage, as the
README warns for the staging clone.

### The ASIF direction

Apple is moving toward **ASIF** (Apple sparse image format) and now lists it
in the Virtualization `VZDiskImageStorageDeviceAttachment` documentation as a
supported disk format and a macOS 26 storage improvement. The repo's
disposable 128 MiB RAW→ASIF→RAW conversion round-tripped byte-for-byte, but
each ASIF image was larger than the source sparse RAW, and in-place
conversion failed. ASIF therefore remains an **isolated conversion
experiment**; the VM source still selects `Disk.img`. Detection here is
heuristic only.

### Base + copy-on-write overlays

The README describes the intended remote-backed model as an immutable base
image (R2 or local), a bounded local read cache, and a durable local writable
overlay. The credential-free block prototype packs an 8 MiB RAW into
SHA-256-checked, zlib-compressed chunks, and a 64 KiB SQLite WAL overlay
preserves unaligned writes. `cloud_disk_checkpoint.py` snapshots overlay
blocks against a pinned base manifest and restores them after verifying
hashes. This toolkit does not implement any of that; it only *inspects* a
base and its overlay files and reports their sizes and capped hashes.

## Relation to existing repo work

- `tools/DISK_COMPRESSION_EXPERIMENT.md` — disposable 32 MiB sparse-RAW
  fixtures, gzip/zstd ratios. It warns that `st_blocks` was **not** used to
  claim sparse savings after writes, and that APFS reported full allocation
  for those fixtures. `fs-inspect.sh` is the read-only counterpart that
  reports the `st_blocks`/`st_size` relationship directly.
- `README.md` → *VM disk and Cloudflare storage* — the 30 GiB / ~25.6 GiB
  allocation figures, the 30 GiB partition layout (macOS APFS ~26.3 GB,
  Recovery ~5.4 GB, security container ~0.5 GB), the `F_PUNCHHOLE` hole lab,
  and the base + cache + overlay research path. `fs-inspect.sh` names and
  reports the same structures.
- `README.md` → *cloud/R2* sections — R2 is an archive, not a writable
  random-access disk; the object-storage NBD path is research. This tool does
  not touch R2 and does not attach anything.

## Safe usage examples

```sh
# A disposable sparse RAW in /tmp
mkfile -n 8m /tmp/vos-fs-test.img    # or: truncate -s 64m /tmp/vos-fs-sparse.img
./tools/fs-inspect.sh /tmp/vos-fs-sparse.img

# A mounted filesystem and its APFS container
./tools/fs-inspect.sh /private/tmp

# A base plus a conceptual overlay / checkpoint
./tools/fs-inspect.sh /tmp/vos-base.img /tmp/vos-overlay.img --no-hash

# Explicitly opting in to inspect inside a bundle (STILL read-only)
./tools/fs-inspect.sh --allow-production ~/VirtualOSMachines/virtualOS.bundle/Disk.img
```

## Limitations

- **Detection is heuristic.** ASIF has no stable on-disk signature the script
  can rely on; APFS is inferred from `file(1)` or the `NXSB` magic. It never
  mounts to confirm.
- **It does not map individual holes.** It reports aggregate sparseness, not
  `SEEK_HOLE` offsets, and it cannot tell allocated-but-zero blocks from real
  data. A production saving estimate still needs a bounded offline scan, as
  the README says.
- **APFS snapshots shown are not cloned or mounted.** Only metadata from
  `diskutil apfs list` is printed.
- **Overlay hashes are capped** at 64 MiB by default to avoid reading a large
  image; they are not content-addressable identities. Use `--no-hash` to skip
  entirely.
- **`hdiutil imageinfo` rejects some formats** (notably some APFS/ASIF
  images); the script tolerates this and continues.
- **No remote path.** It never talks to R2 or any network store.
- **Read-only by construction.** There is no code path that opens a file for
  writing, attaches a device, or runs a mount command.
