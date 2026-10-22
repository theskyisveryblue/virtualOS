# virtualOS Cloud Disk — network-booted storage (experimental)

Goal: let a Mac boot its disk from **somewhere else** — content-addressed
objects, a raw file, or Cloudflare R2 — with a bounded local cache and overlay,
so the disk can outlive the machine and the machine can move between Macs.

**Status: a full macOS guest boots and runs from a network disk.** With no local
`Disk.img` at all, a `VZMacPlatformConfiguration` + `VZMacOSBootLoader` guest read
its boot volume over loopback NBD and rendered a real desktop:

```
netboot=NBD_verified reads=47832 read_bytes=858846720 writes=3585 write_bytes=24772608 errors=0
```

Reproduce with `tools/fleet/runbook.sh netboot vm-clone vm-net` — it serves a
bundle's RAW over NBD, prepares a disk-less bundle that points at it, boots under
a GUI-session agent, captures the desktop, and tears down; the source RAW is
never modified and every guest write lands in a `/private/tmp` overlay.

This is experimental and does not change the shipping single-VM app. The
executable interfaces are **`tools/computer-space.sh`** (storage plane) and
**`tools/fleet/runbook.sh`** (fleet + netboot); this document explains them.

## The shape of a disk

A disk is stored twice over:

- **Base image** — immutable, split into 4 MiB `CHUNK`s, each compressed and
  addressed by the SHA-256 of its *uncompressed* bytes. Identical chunks collapse
  to one object (`virtualos-cloud-disk/v1` manifest lists the chunks and the
  logical size).
- **Overlay** — a private SQLite file holding only the blocks this machine wrote.
  Reconnecting reopens the same overlay, so writes survive a network drop.

A small in-memory/temp **cache** holds recently fetched chunks, capped by
`local_cache_limit_bytes` and required to leave a host-reserve free.
The persistent chunk cache is trimmed to that budget when the disk service
opens, as well as after new downloads. Reducing the limit therefore reclaims
old cached chunks on restart even when subsequent reads only hit the cache.
Trimming preserves symlinks, directories, unrelated metadata, and the writable
overlay; evicted base chunks can be fetched again from their immutable source.

## Serving it

`tools/cloud_disk.py` speaks **NBD** on loopback. A VM attaches with
`VZNetworkBlockDeviceStorageDeviceAttachment(url: nbd://127.0.0.1:40809/<export>)`.
Backends:

| Backend | Source of truth | Command |
| --- | --- | --- |
| `objects` | packed local objects | `cloud_disk.py lab-serve ROOT OVERLAY PORT` |
| `raw` | a raw file | `cloud_disk.py raw-lab-serve SOURCE OVERLAY PORT` |
| `bundle` | a named fleet bundle's RAW | `cloud_disk.py bundle-serve ROOT NAME OVERLAY PORT` |
| `base+delta` | a shared base bundle's RAW **plus a seeded per-machine delta** | `cloud_disk.py base-delta-serve ROOT BASE OVERLAY PORT DELTA_SQLITE` |
| `r2-fake` | packed objects through the R2 read layer (fake HTTP) | `cloud_disk_vz_fake.py ROOT OVERLAY CACHE PORT` |
| `r2` | real Cloudflare R2 | `cloud_disk.py r2-serve CONFIG MANIFEST_SHA256 40809` |

The R2 read layer is **read-only and SigV4-signed**, pinned to one account,
bucket, and object prefix; it fetches only `manifest.json` and SHA-addressed
chunks, rejects any hash mismatch, and never writes to R2.

## Running the labs

All labs are disposable, loopback-only, and need no credentials:

```bash
# full storage self-test (25 checks: overlay, cache reserve, corruption, reconnect…)
tools/computer-space.sh --cloud-disk-self-test

# overlay checkpoint/restore self-test
tools/computer-space.sh --cloud-disk-checkpoint-self-test

# boot a real EFI guest from an NBD disk, per backend
VOS_NBD_VZ_BACKEND=objects tools/computer-space.sh --nbd-vz-lab
VOS_NBD_VZ_BACKEND=raw     tools/computer-space.sh --nbd-vz-lab
VOS_NBD_VZ_BACKEND=r2-fake tools/computer-space.sh --nbd-vz-lab

# sparse/hole handling (never touches the production disk)
tools/computer-space.sh --raw-hole-lab
```

The focused `tools/test_cloud_disk_checkpoint.py` suite also verifies a snapshot
taken while the source SQLite connection remains active: later source writes
stay intact, and restoring into a new overlay reproduces the earlier bytes.
Restores refuse existing destinations. Checkpoint manifest/object reads inspect
owned regular file descriptors with no-follow and nonblocking opens; FIFOs,
symlinks, hard links and shared-writable inputs are refused. Remote manifest
hashes are checked before fetching any referenced objects. These are storage
checks; automatic rollback of a running guest remains unverified.

`--nbd-vz-lab` compiles a tiny Swift probe, serves a 16 MiB disposable image over
NBD, attaches it to a generic-platform VM, and asserts that **Apple's client**
negotiated NBD and performed real reads. All three backends pass.

## Real R2 — verified

`--cloud-disk-r2-boot-lab` does the same against **real Cloudflare R2**:

```bash
source ~/.config/virtualos/r2.env        # AWS keys + CLOUDFLARE_ACCOUNT_ID/ENDPOINT
export VOS_CLOUD_DISK_CONFIG=~/.config/virtualos/r2-lab-config.json
tools/computer-space.sh --cloud-disk-r2-boot-lab
# r2_boot_lab=real_r2_verified reads=33 bytes=24576 cold_us_max=...
```

It builds a 16 MiB image (three patterned chunks + one zero chunk), uploads it as
content-addressed objects, serves it over NBD via `cloud_disk.py r2-serve`, boots
a generic EFI guest, and verifies the NBD connection negotiated and served reads.
Observed: a cold chunk fetched from R2 costs hundreds of milliseconds, then reads
are served from the bounded local cache in microseconds. `writes_to_r2` stays 0 —
the disk is read-only at the source; all writes land in the local overlay.

### A full macOS disk over R2 (the remaining scale step)

#### Convert the existing backup without extracting a full local disk

`cloud_disk_archive.py:stream_archive_upload` accepts a sequential private
tar.gz stream and the existing R2 upload-store interface. It emits verified
4 MiB base chunks and publishes the boot manifest only after the entire
compressed archive and reconstructed logical disk match their size and SHA-256
pins. It never extracts a local RAW or compressed archive. Conditional object
writes support reuse on a restarted conversion; gzip must still be reread from
the beginning.

The delegated Mini-to-R2 conversion completed on October 19: all 7,680 chunks
passed readback, the 22,266,108,635-byte archive and 32,212,254,720-byte raw disk
matched their full SHA-256 pins, and the immutable manifest was published.
Fresh verified uploads totalled 18,210,858,297 bytes, with 4,056,274,597 bytes
reused. No full disk or archive was extracted locally. See the
[conversion receipt](experiments/r2-archive-base-conversion-20261019.json).

Continue the read-only observer and worker with their two no-argument commands:

```sh
./tools/cloud-disk-observe.sh
./tools/cloud-disk-archive.sh
```

The worker independently reread and hashed the full raw disk from R2, matching
all 32,212,254,720 bytes against the pinned SHA-256 at 04:36 UTC on October 19.
This pass is separate from per-chunk conversion readback. Its private Mini
receipt was independently checked; capacity retries reuse the successful audit.
See [`r2-independent-base-readback-20261019.json`](experiments/r2-independent-base-readback-20261019.json).
A macOS recovery boot still needs a free slot. Both existing macOS guests stay
running, and `guest_boot_verified` remains false.

The actual Ai backup uses GNU sparse 1.0 metadata. On the Mini's Python 3.9.6,
the default tar parser reports 27,452,520,448 stored bytes as the disk size,
although the sparse header declares a 32,212,254,720-byte logical disk. Its
next-header offset also counts the sparse-map storage twice. The converter
restores the logical size, validates sparse extents, and advances by their
stored bytes. A read-only range probe of at most 4 MiB from the real archive
verified the corrected header; it did not verify the full disk hash or boot.

Regression fixtures cover ordinary and sparse disks, holes, following headers,
hash failures, truncation, duplicates, corrupt chunk readback, immutable
manifest conflicts and bounded traced allocations. They run with:

```sh
python3 -m unittest discover -s tools -p test_cloud_disk_archive.py
```

The existing archive's full-download audit is separate evidence. Full base
conversion and independent full base readback have now passed. A macOS guest
boot from this R2 base remains a gate before removing either running VM's local
disk; this review retired neither disk.

#### Original stopped-clone upload path

Serving a **macOS** guest requires the disk to be attached over NBD while the
platform is `VZMacPlatformConfiguration` + `VZMacOSBootLoader` (macOS does not
boot on the generic platform). That combination is **verified valid** via
`--nbd-r2-macos-config-check`: it loads the production `HardwareModel` and
`MachineIdentifier`, uses a disposable copy of the auxiliary storage, points the
disk at NBD, and validates the configuration without booting:

```
nbd_macos_config=valid source=metadata_only disk=nbd aux=disposable_copy boot_started=false
```

What remains is only the scale move — a crash-consistent clone of the ~30 GiB
production RAW. That requires the production VM to be stopped (a live copy is not
crash-consistent), so it is operator-gated:

1. `computer-space.sh --nbd-r2-metadata-prepare` — platform data only, no RAW copy.
2. Stop production; APFS copy-on-write clone the RAW to
   `.virtualOS-r2-staging.bundle/Disk.img`; write the `.complete` marker.
3. `VOS_CLOUD_DISK_UPLOAD_ACT=1 computer-space.sh --cloud-disk-r2-upload-clone`.
4. `cloud_disk.py r2-serve CONFIG <manifest_sha256> 40809`, then boot with the
   macOS platform + NBD attachment (`--nbd-macos-clone-test` automates 2–4 and
   restores production at the end).

Everything except the stop/upload of the full disk is already verified on real
hardware and real R2.

## Minting R2 S3 credentials from an API token

R2 S3 keys are derived from a Cloudflare API token:

- **Access Key ID** = the token `id`
- **Secret Access Key** = hex SHA-256 of the token `value`

Create a bucket-scoped token (read + write) with the R2 permission-group IDs:

```
permission group id (read):  6a018a9f2fc74eb6b293b0c548f38b39
permission group id (write): 2efd5506f9c8494dacb1fa10a3e7d5b6
resource: com.cloudflare.edge.r2.bucket.<ACCOUNT_ID>_default_virtualos-cloud-disks
```

```bash
POST https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/tokens
Authorization: Bearer <broad-scoped-token>
{"name":"virtualos-cloud-disk-lab","policies":[{"effect":"allow",
 "resources":{"com.cloudflare.edge.r2.bucket.<ACCOUNT_ID>_default_virtualos-cloud-disks":"*"},
 "permission_groups":[{"id":"<read>"},{"id":"<write>"}]}]}
```

The token `value` is shown once; the secret is `sha256(value)` hex. Store both in
a 0600 file (e.g. `~/.config/virtualos/r2.env`) and never in the repo.

## Real R2 checkpoint (durability) — verified

`--cloud-disk-r2-checkpoint-lab` proves the *write/durability* half against real
R2: it uploads a disposable base, writes two blocks into a local SQLite overlay,
snapshots the overlay into a content-addressed checkpoint, uploads the
checkpoint objects (manifest last) under its own `<prefix>/checkpoints/<sha>`
prefix, then restores from real R2 into a fresh overlay and verifies the blocks
and base identity match byte-for-byte.

```bash
tools/fleet/runbook.sh cloud checkpoint
# {"r2_checkpoint_lab":"real_r2_verified","blocks":2,"restored_blocks":2,
#  "identity_matches":true,"manifest_last":true,...}
```

Rerunning is idempotent: immutable objects are reused (`uploaded_bytes:0`) and the
manifest is written last, so a crash mid-upload never publishes a partial
checkpoint.

## A full macOS disk over R2 (the remaining scale step)

A real R2 boot is deliberately gated and not yet run end-to-end:

1. **Credentials** — `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` pinned to the
   account, plus `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_R2_ENDPOINT`
   (`...r2.cloudflarestorage.com`). Bucket `virtualos-cloud-disks` only.
2. **Isolated clone** — an APFS copy-on-write clone of a *stopped* bundle at
   `~/VirtualOSMachines/.virtualOS-r2-staging.bundle/Disk.img` with a `.complete`
   marker. The uploader refuses the production disk and refuses an open disk.
3. **Upload** — `cloud_disk.py r2-upload-clone CONFIG` streams the clone into
   content-addressed objects and writes `manifest.json` last.
4. **Serve + boot** — `cloud_disk.py r2-serve CONFIG MANIFEST_SHA256 40809`, then
   boot a VM whose storage attachment points at that NBD endpoint.

A macOS guest can be served this way (it uses `VZMacPlatformConfiguration` +
`VZMacOSBootLoader`, and macOS only needs the disk over NBD to match the original
bytes). `computer-space.sh --nbd-macos-clone-test` automates the whole thing but
**stops the production VM**, so it must not be run unattended.

## Base + delta served as one disk (the compact-fleet step)

A fleet should keep **one immutable base** and a small per-machine delta, not N
full disks. `base-delta-serve` makes that a first-class disk backend: it serves a
named bundle's RAW as the immutable base and seeds the machine's delta (a
`shared_base.py diff` sqlite) into the bounded overlay **before** the guest
attaches, so a guest that reads this NBD sees the machine, not the base.

```bash
# 1. the delta (read-only; see tools/experiments/shared_base.py)
python3 tools/experiments/shared_base.py diff \
    ~/vos-fleet/base.bundle/Disk.img ~/vos-fleet/machine.bundle/Disk.img \
    /private/tmp/vos-exp-shared-base/delta

# 2. serve base + that delta as one disk
VOS_NBD_BUNDLE_ROOT=~/vos-fleet VOS_NBD_BASE_NAME=base \
VOS_NBD_BUNDLE_OVERLAY=/private/tmp/vos-nbd-bundle-machine/overlay.sqlite \
VOS_NBD_BUNDLE_PORT=40951 VOS_NBD_BASE_DELTA=/private/tmp/vos-exp-shared-base/delta/delta.sqlite \
tools/computer-space.sh --nbd-base-delta-serve
```

The base is never written (`writes_to_base: 0`); only the overlay grows, bounded
by the delta plus whatever the guest later writes. The first seed records a
content hash of the delta in the overlay, in the same transaction as its blocks.
Restarting with the same delta preserves guest edits, including zeroed blocks.
A different delta is refused. Seeding validates block bounds and lengths,
enforces the overlay budget and host reserve, and streams a consistent SQLite
snapshot instead of loading the entire delta into memory.
Nonempty overlays from earlier versions lack the delta pin and are refused;
retain them for explicit migration.
`tools/experiments/base_delta_serve_test.py` proves it end to end: it starts the
listener, negotiates `NBD_OPT_GO` as a real client, and asserts a block that the
machine changed reads back as the machine's block. It then writes through NBD,
terminates the server, restarts it against the same overlay, and verifies that
guest edits and zeroed blocks survive. It is the
11th check in the fleet self-test.

The host-free reserve is 10 GiB by default; `VOS_NBD_MIN_HOST_FREE` lowers it for
a bounded lab on a full host without weakening the production default.

### Store a machine delta in R2

The R2 delta commands use the same pinned bucket and credentials as the cloud
disk commands. Give each machine its own config `object_prefix`, such as
`fleet/machine-a`; the delta manifest at that prefix is immutable. Reusing a
prefix with different delta contents fails instead of replacing a machine.
The config contains no credentials; set the account and R2 keys in the
environment as described above.

Set `VOS_R2_DELTA_WORKERS` to 1–16 for bounded parallel block upload/download;
the default is one worker. Each upload verifies its conditional write by reading
the object back, and the manifest is published only after every block passes.
`VOS_R2_DELTA_PROGRESS=1` emits block counts every 256 blocks.

```bash
export VOS_CLOUD_DISK_CONFIG=~/.config/virtualos/machine-a-r2.json
export VOS_DELTA_SQLITE=/private/tmp/vos-exp-shared-base/delta/delta.sqlite
tools/computer-space.sh --cloud-disk-r2-upload-delta

# On the destination host, fetch and verify the pinned delta before NBD boot.
# Use delta_manifest_sha256 from the upload command's JSON receipt.
export VOS_DELTA_MANIFEST_SHA256="paste-64-hex-sha256-from-upload"
export VOS_DELTA_SQLITE=~/vos-fleet/deltas/machine-a.sqlite
mkdir -p ~/vos-fleet/deltas
tools/computer-space.sh --cloud-disk-r2-pull-delta
```

Upload writes compressed content-addressed blocks first and the manifest last.
It reads each object back and checks its SHA-256. Pull validates the manifest and
every block, then atomically installs the SQLite delta; a failed pull leaves an
existing local delta intact. The manifest hash pin prevents a different delta
at that prefix from being accepted. The base RAW is a separate immutable artifact on
the host. `--nbd-base-delta-serve` consumes the fetched SQLite through
`VOS_NBD_BASE_DELTA`.

For a delta already on the Mini, the same `.sh` entry points support
`VOS_DELTA_SOURCE_HOST=mac-mini-ai`. `VOS_DELTA_SQLITE` then names a path on that
host. The operator signs individual requests while the Mini sends or receives
bytes directly through HTTPS to the pinned R2 bucket. Secret keys are removed
from the SSH child environment; the Mini receives signatures scoped to the
selected prefix, method, and payload hash. Upload pairs each write signature
with its readback signature, and the Mini reuses verified TLS connections.
Recovery permits GET requests only and preserves an existing destination on a
failed manifest pin or block verification.

The remote host must have `cloud_disk_r2_delegate.py` and the disk tools under
`/Users/ai/vos-shared-fleet/tools`, or the directory named by
`VOS_R2_REMOTE_TOOLS`. The operator and remote worker use stdin/stdout for request
metadata and progress; bulk data and secret keys do not cross that SSH channel.
The source and recovered block sets produce an index-and-bytes SHA-256 for an
independent whole-delta comparison. A real 25,404-block delta has now completed
that comparison after upload and recovery from the Ai account. The recovered
delta also reconstructs all 491,520 disk blocks with zero mismatches and boots
twice through the managed NBD path, including a disk-service restart. Both login
frames were visually checked. Evidence is in
`tools/experiments/r2-machine-delta-receipt.json`,
`tools/experiments/r2-recovered-verify-receipt.json`, and
`tools/experiments/r2-recovered-boot-receipt.json`.
The inventory receipt (`tools/experiments/r2-machine-storage-receipt.json`)
measures 824,983,105 stored bytes for 25,376 unique blocks plus the manifest.
The completed test's local base, seed, and overlay have since been removed to
reclaim space. Its cloud catalog is retained under `cloud-machines/` on the Mini
and in `tools/experiments/r2-cold-machine-catalog.json`. Restoring the test requires
the archived base and pinned delta; the two running VM disks remain local.
`go-live.sh` refuses to clone production for a cold cloud machine without its
restored shared-disk configuration. Full archive guest restore is still separate.

Delta manifests have a separate 16 MiB upload bound. The real machine's 5.12 MB
manifest exposed an older check that applied the 4 MiB RAW chunk bound to all
objects. That check is fixed, and the signed-HTTP regression now covers a
25,404-block manifest and rejects oversized objects and manifests.

Run a bounded real-R2 transport check with credentials already exported:

```bash
python3 tools/cloud_disk_delta_probe.py ~/.config/virtualos/r2-lab-config.json
```

The probe uses a fresh `fleet/delta-probe-…` prefix and three synthetic 64 KiB
blocks. It checks exact recovered bytes, conditional object reuse, and rejection
of a wrong manifest pin while preserving the existing local delta. It retains
four small remote objects as evidence and removes its temporary local files.
On 2026-10-11 Toronto time, the real R2 check passed with 196,608 raw bytes and
66,177 compressed bytes. The receipt is in
`tools/experiments/r2-delta-probe-receipt.json`. This proves the delta transport, not a
real machine delta, VM boot, or full VM archive recovery.

## Prepare an existing shared-disk machine

`tools/fleet/go-live.sh vm-a --prepare-only` validates an existing machine bundle
and registers it under the fleet's `machines` directory without starting a VM,
supervisor, or relay. A diskless bundle must provide a valid `nbdURL` in
`Parameters.txt`, plus `HardwareModel` and `MachineIdentifier`. Existing bundles
with invalid parameters or missing platform files are refused and preserved.

The normal `go-live.sh vm-a` path preserves NBD configuration and checks `vm-a`
specifically before requesting a start. For a managed shared disk, create
`<fleet>/shared-disks/vm-a.json` (or set `VOS_SHARED_DISK_CONFIG`):

```json
{"baseName":"base","platformName":"vm-source","delta":"deltas/vm-a.sqlite","port":40991}
```

The named base and platform bundles must already exist in the fleet root. The
platform source provides `HardwareModel`, `MachineIdentifier`, and
`AuxiliaryStorage`; omit `delta` when the new machine should use the base alone.
For a new machine, `go-live.sh` copies only these platform files and writes an
NBD parameter, avoiding another full disk. Existing local-disk bundles require
an explicit migration and are preserved. Changed configurations are refused once
the machine has been prepared.

New machines without shared storage are refused by default. An intentional
full local clone requires `VOS_ALLOW_LOCAL_CLONE=1`; this is not a compact
storage mode, because independent guest writes can accumulate a full disk's
worth of physical blocks over time.

Overlay writes retain only actual differences when the original bytes are
locally available. Restoring a RAW-base block removes its overlay row; cloud
disks compare only decoded cached chunks or manifest-declared zero chunks, so
a full-block write still works without fetching the base. Capacity is checked
against the net changed-block count for the complete write. Deleted rows make
SQLite pages reusable; physically shrinking an existing database still needs
offline compaction. `python3 tools/experiments/compact_overlay_measure.py`
demonstrates that distinction using disposable synthetic data only.

For a compressed immutable initial delta, use `compressed_delta.build` with
frozen scratch base/machine copies, then configure the resulting artifact:

```json
{"baseName":"base","platformName":"vm-source","delta":"deltas/vm-a.compressed.sqlite","deltaFormat":"compressed-v1","deltaSHA256":"<reviewed artifact SHA-256>","port":40991}
```

The base identity and full compressed artifact hash are pinned. The NBD backend
reads compressed initial blocks directly and puts only subsequent changes in
the writable overlay. Restoring initial bytes removes that later overlay row.
This avoids both a raw seed copy and consuming the 2 GiB write budget before
boot. Legacy seeded SQLite deltas still work. A plain RAW backend refuses the
new layered overlay identity, preventing an accidental start without its seed.

The October 20 scan of the two current 32,212,254,720-byte disks found
34,409 differing blocks: 2,255,028,224 raw bytes, or 1,168,285,619 bytes of
level-1 compressed block payload before database/manifest overhead. These are
short-lived COW analysis copies, not verified migration snapshots or measured
unique APFS savings. The new backend passed 43 Mini regressions plus the full
disk-plane self-test; the running disks have not been migrated.

`go-live.sh` writes the disk LaunchAgent plist under the machine's state directory,
loads it into the GUI launchd domain, and waits for readiness from that process
plus a real NBD greeting before starting the supervisor or VM. `KeepAlive`
restarts a failed server. Repeated calls reuse the running service. The existing
`runbook.sh live vm-a --prepare-only` command prepares files without loading it.
`--boot-only` runs the machine and disk phases without starting relay services.
The default NBD startup wait is 180 seconds to cover gigabyte-scale seeding;
`VOS_NBD_START_TIMEOUT` accepts 1–600 seconds. The running supervisor registers
new prepared machines when `runbook.sh start <name>` requests them.
Run the isolated preparation/start checks with
`bash tools/fleet/go-live.sh --self-test`.

The existing `computer-space.sh --nbd-base-delta-serve` entry point also supports
`VOS_NBD_PERSISTENT=1`. In that mode, set `VOS_NBD_BUNDLE_OVERLAY` to
`<VOS_NBD_BUNDLE_ROOT>/.virtualos-nbd/<machine>/overlay.sqlite`. The server keeps
waiting for VM connections beyond the lab's eight-session cap. It locks the
overlay for its lifetime, rejects symlinked state directories, and validates the
base identity before reporting readiness. The base remains read-only and the
overlay retains the 2 GiB write limit and default 10 GiB host reserve.

`./tools/fleet/runbook.sh cloud shared-base` exercises both lab and persistent
modes, including nine connections, refusal of a second server using the same
overlay, and preservation of guest writes across full server restarts.
The persistent mode stores state outside temporary storage when the fleet root
is durable. The managed boot workflow passed two real macOS guest boots on the
Mini, including a complete NBD service restart with zero blocks reseeded. Both
source disks stayed unchanged, the machine had no local `Disk.img`, and its
overlay occupied 1,694,564,352 bytes. Both boots reached the login screen, which
was visually checked. Receipts are in
`tools/experiments/managed-shared-boot-receipt.json` and
`tools/experiments/managed-shared-verify-receipt.json`.

`tools/experiments/boot_from_delta.sh` reproduces this using stopped snapshots in
an isolated fleet root. It verifies the full delta, boots through `go-live.sh`,
restarts the disk service, boots again, and checks source file identities. Set
`VOS_EXP_RELEASE_VM=vm-a` only when borrowing that fleet VM's slot; its exit trap
restores the VM and watchdog, including failure paths. Test services are stopped
afterward, while the overlay and receipts remain for review. A login screen is
sufficient for this storage boot check; the runner uses a 30 KB frame gate, and
normal `runbook.sh desktop` retains its 100 KB default.

To test an R2-recovered delta, pull into a new isolated output directory and set
`VOS_EXP_OUT` to that directory for `boot_from_delta.sh`. When `delta.sqlite`
already exists, the runner verifies that file against the stopped source
snapshots instead of regenerating it. Export metadata (`delta.json`) is not
required. The boot receipt includes `initial_delta_sha256`, which must match the
R2 recovery receipt's `source_delta_sha256` before reporting a recovered guest.
The shared base is still required on the Mini; a verified delta alone does not
prove a complete cloud archive.

## Guardrails (do not remove)

- Credentials are environment-only; the config file never holds them
  (`"credentials": "environment_only"`).
- Source pins are validated: account `[0-9a-f]{32}`, endpoint, bucket, prefix,
  and a 64-hex manifest hash.
- The production RAW disk is never uploaded or modified; labs use disposable
  images and print `production_disk_touched: false`.
- A network disk is not "done" until it boots, restarts, survives a disconnect,
  and restores — the self-tests cover corruption, reconnect, and overlay restore.

## Known fix

`cloud_disk_vz_fake.py` previously built its fake config with bucket
`virtualos-test`, which the pinned R2 read layer rejects, and served a blocking
`accept()` on every `open_disk` call (the self-test calls it twice to check cache
reuse), so the `r2-fake` lab hung. It now uses the canonical bucket and serves
NBD once; all three backends pass.
