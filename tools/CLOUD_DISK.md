# virtualOS Cloud Disk — network-booted storage (experimental)

Goal: let a Mac boot its disk from **somewhere else** — content-addressed
objects, a raw file, or Cloudflare R2 — with a bounded local cache and overlay,
so the disk can outlive the machine and the machine can move between Macs.

This is experimental and does not change the shipping single-VM app. The
executable interface is **`tools/computer-space.sh`**; this document explains it.

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

## Serving it

`tools/cloud_disk.py` speaks **NBD** on loopback. A VM attaches with
`VZNetworkBlockDeviceStorageDeviceAttachment(url: nbd://127.0.0.1:40809/<export>)`.
Backends:

| Backend | Source of truth | Command |
| --- | --- | --- |
| `objects` | packed local objects | `cloud_disk.py lab-serve ROOT OVERLAY PORT` |
| `raw` | a raw file | `cloud_disk.py raw-lab-serve SOURCE OVERLAY PORT` |
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

### Minting R2 S3 credentials from an API token

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
