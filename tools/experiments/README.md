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

# Or run both at once
tools/experiments/run.sh
```

`report` prints `fleet_saving_pct`: how much smaller one shared copy is than
storing every machine's disk independently. `diff` writes `delta.sqlite` (only
the changed 64 KiB blocks) plus `delta.json`, which is exactly what a machine
needs on top of the shared base to reproduce itself.
