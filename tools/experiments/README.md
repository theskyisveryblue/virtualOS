# virtualOS experiments (ISOLATED)

This directory is for **novel, experimental** disk work that is deliberately
kept separate from the stable disk plane we rely on.

## Separation rules

- **Stable disk plane is frozen.** `tools/cloud_disk.py`, `tools/cloud_disk_*.py`,
  `tools/computer-space.sh`, and the `runbook.sh cloud …` commands are the stable,
  verified disk system. Experiments here do not modify them and do not depend on
  their state.
- **Own namespace.** Anything an experiment pushes to Cloudflare R2 uses its own
  prefix under `experiments/…` in the **ai@damidina.com** account
  (`c24a3fb4bedba669785c18c834f51921`), never the prefixes the stable disk uses
  (`disks/virtualos-*`).
- **Own storage, read-only to everything else.** Experiments read clone images
  read-only and write all mutable state (objects, overlays, deltas) under
  `/private/tmp` or an explicit `VOS_EXP_OUT` directory.
- **Production is off limits.** `~/VirtualOSMachines` (the live machine store) is
  refused outright; experiments use their own APFS COW clones.

## Experiments

### `shared_base.py` — one base, many machines

The fleet should not store N full disks. Real clones of the same source share
almost every block, so the fleet can keep **one immutable base** plus a **tiny
per-machine delta**.

```bash
# Block-level sharing + fleet storage estimate (read-only)
python3 tools/experiments/shared_base.py report \
    ~/vos-fleet/vm-clone.bundle/Disk.img ~/vos-fleet/vm-clone2.bundle/Disk.img

# Materialise a machine's difference from the base as a block overlay
python3 tools/experiments/shared_base.py diff \
    ~/vos-fleet/vm-clone.bundle/Disk.img \
    ~/vos-fleet/vm-clone2.bundle/Disk.img \
    /private/tmp/vos-exp-shared-base/delta-vm-b

# Prove base + delta reproduces the machine byte-for-byte (no materialisation)
python3 tools/experiments/shared_base.py verify \
    ~/vos-fleet/vm-clone.bundle/Disk.img \
    ~/vos-fleet/vm-clone2.bundle/Disk.img \
    /private/tmp/vos-exp-shared-base/delta-vm-b

# Fast synthetic round-trip guard (no real disks)
python3 tools/experiments/shared_base.py selftest

# Or run everything (selftest + report + diff + verify)
tools/experiments/run.sh
```

`report` prints `fleet_saving_pct`: how much smaller one shared copy is than
storing every machine's disk independently. `diff` writes `delta.sqlite` (only
the changed 64 KiB blocks) plus `delta.json`, which is exactly what a machine
needs on top of the shared base to reproduce itself. `verify` streams the base
and machine and asserts that delta-over-base equals the machine block-for-block —
it does not materialise the reconstructed disk, so it needs no extra space next
to a 30 GiB image.

## Measured on the Mac mini (real disks)

`verify` run against the production macOS disk as base and the fleet machine
`vm-a` as the machine (both read-only APFS COW clones under `/private/tmp`):

```
base       prod macOS disk   32,212,254,720 B   491,520 blocks
machine    vm-a              32,212,254,720 B
delta      20,326 blocks     1,332,084,736 B    (4.14% of blocks, 4.14% of bytes)
verify     blocks_checked 491,520   mismatches 0   stray_delta_blocks 0
           round_trip_ok true     (base + delta == vm-a, byte-for-byte)
```

So a second machine derived from the shared base costs **~1.33 GB** of delta
instead of a full **30 GiB** copy — a 95.9% saving — and the reconstruction is
proven exact, not asserted. (This is the *raw* delta before compression; the
stable disk plane compresses the same changed blocks further.)

## Serving base + delta as one disk

`tools/experiments/base_delta_serve_test.py` turns the proof into a *bootable*
disk: it serves a base bundle's RAW over NBD with the machine's delta seeded into
the overlay, then connects as a real NBD client and asserts a changed block reads
back as the machine's. The stable plane exposes this as
`computer-space.sh --nbd-base-delta-serve` (see `tools/CLOUD_DISK.md`), and it is
the 11th check in `tools/fleet/self_test.sh`.

```bash
python3 tools/experiments/base_delta_serve_test.py
# {"base_delta_serve_test": "passed", "block3_is_machine_block": true,
#  "delta_seeded": 2, ...}
```

