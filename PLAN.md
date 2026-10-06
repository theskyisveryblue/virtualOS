# virtualOS — the big plan

> One Mac runs **many computers**, hands each a **whole machine of its own**, keeps
> their disks **tiny and shareable**, lets an **AI agent drive them without asking
> anyone for permission**, and reaches them **from anywhere**. This is the plan to
> get from what we have proven today to that, in the open, measuring every step.

## The thesis in one line

A computer should be a *cheap, disposable, remotely-reachable object*, not a
30 GiB fixture chained to one Mac. Everything below serves that sentence.

## What is already true (earned, not claimed)

These are measured results in this repo, not intentions:

- **Multi-VM on one Mac.** A headless supervisor runs several VMs, queues when
  Apple's 2-active-VM host cap is hit, and promotes queued VMs automatically.
- **Disks as a service.** A full macOS guest boots with **no local `Disk.img`**,
  reading its boot volume live over NBD while every write lands in a bounded
  `/private/tmp` overlay; the source image is never touched.
- **Cloud durability.** Checkpoints round-trip to real R2 with identity verified;
  a 16 MiB image boots a guest straight from R2 over signed reads.
- **Tiny fleet disks.** Two real 30 GiB clones collapse to **7,282 unique chunks
  vs 13,371** (~45% less); the second machine differs from the base by
  **222.7 MB = 0.69%**. A machine booted from **one shared base + a 223 MB delta**.
- **A second OS.** A real **Alpine Linux 6.6.134 aarch64** guest boots to an
  interactive shell on Virtualization.framework (~10 MB of boot artifacts).
- **Remote, live, multi-host.** The fleet streams to a Cloudflare worker; a browser
  view shows status, live frames, and start/stop; heartbeats are verified fresh.
- **Self-healing live stack.** One idempotent command (`runbook.sh live`) converges
  supervisor + VM + stream loop + relay.
- **Guest input works.** Single-guest `/console` drives the guest's own
  keyboard/mouse and returns live frames.

## What is not true yet (the honest gap)

- **Nested VMs.** The host advertises `isNestedVirtualizationSupported = true`,
  but the guest kernel logs `kvm [1]: HYP mode not available` — **no EL2/HYP is
  handed through**. VM-in-VM is blocked by the platform today, not by our code.
- **Windows.** No Windows path (VMPal uses its own engine; we have not built one).
- **Fleet input parity.** The multi-VM web view streams frames but does not yet
  drive per-VM keyboard/mouse the way the single-guest console does.
- **Concurrency.** One host caps at 2 active VMs; we queue but do not yet *scale
  across Macs*.
- **Speed of storage.** Full-disk cloud boot over the measured **0.44 MB/s** link
  is ~15 h — cloud boot must go through deltas, never brute force.

## The one number that matters

**Cost to add a computer.** Today a macOS machine is ~30 GiB; our shared-base
result says the *incremental* machine is **~0.69% (~223 MB)**; a Linux machine is
**~10 MB**. The plan drives this number down and makes everything else follow.

---

## Pillars

### 1. Many computers, one Mac (density)
- **Goal:** N machines per host, each isolated, each its own Dock-like identity.
- **Already:** supervisor + capacity queuing.
- **Next:** finish input parity per VM; pin self-healing via a watchdog; measure
  cold/warm boot time and memory per VM; define an honest per-host budget.
- **Metric:** machines running concurrently; seconds to boot; RSS per machine.

### 2. Disks that are objects, not fixtures (storage)
- **Goal:** a machine's disk is a small, content-addressed, shareable object.
- **Already:** chunk disk format, NBD serving, R2 checkpoints, shared base+delta,
  netboot with no local disk.
- **Next:** adopt **base + per-machine delta as the default** (not a lab); make the
  delta the unit of snapshot/rollback; deduplicate across the fleet; store deltas
  under an `experiments/`… no — a first-class `fleet/` namespace in R2.
- **Metric:** bytes to store machine #K; snapshot cost; roll-back latency.

### 3. Machines the agent can drive (agency)
- **Goal:** an agent uses a VM's **own keyboard and mouse** — the OS sees a user,
  not an app asking for Accessibility/Screen Recording.
- **Already:** single-guest input + live frames; `inputctl` exists in the stream
  server; the AHA "stop clicking Allow" idea is the design target.
- **Next:** per-VM input on the relay; a small command/verb surface (click, type,
  key, scroll, wait-for-frame); snapshots as the undo bound for agent runs.
- **Metric:** a scripted agent task completed end-to-end in a VM with zero host
  permission prompts.

### 4. Reachable from anywhere (remote)
- **Goal:** every machine is a URL, not a local window.
- **Already:** Cloudflare relay, `/fleet/status`, `/fleet/frame/:vm`, `/console`,
  browser fleet view, tunnel script.
- **Next:** per-VM control channel; auth model for fleet ops; R2 as the frame/
  snapshot store behind the edge; a single "open a machine" link.
- **Metric:** time-to-first-frame from a cold client; control round-trip latency.

### 5. More than macOS (breadth)
- **Goal:** the box runs macOS, Linux, and — if reachable — Windows.
- **Already:** macOS booted; Alpine Linux booted (~10 MB).
- **Next:** Linux rootfs + persistence (so the tiny guest is usable, not just
  bootable); try nested again *properly* (documented reproduction, try configs,
  maybe newer macOS, maybe a Linux guest that exposes EL2); evaluate a
  guest-hosted hypervisor only where the platform truly allows it.
- **Metric:** OSes bootable per host; artifact size per OS.

### 6. Honest, measured, self-healing (engineering discipline)
- **Goal:** nothing here is a claim; everything is a re-runnable measurement.
- **Already:** MANIFESTO of earned challenges; `runbook.sh` self-tests; guards
  that refuse the production store; labs that print verdict JSON.
- **Next:** a single top-level `plan-status.sh` that runs every lab and prints
  the whole scoreboard; a watchdog that keeps the live stack converged; CI-ish
  self-tests for the fleet and storage planes.
- **Metric:** every pillar has a command that prints its number.

---

## Phases

**P0 — Stay live (now).** Keep the current stack converged; add a watchdog so the
relay never goes stale. *Exit:* `plan-status.sh` shows live + heartbeats fresh.

**P1 — Compact fleet.** Make shared base+delta the default disk; boot machine B
from base+delta in the normal path; store deltas in R2; measure bytes/machine.

**P2 — Agent control.** Per-VM input on the relay + a small verb surface +
snapshot-as-undo; complete a scripted task inside a VM with no host prompts.

**P3 — Breadth.** Persistent Linux guest; honest nested re-attempt with a
documented reproduction matrix; Windows only if a real path appears.

**P4 — Multi-host.** Scale past the 2-VM cap with a second Mac; fleet scheduler;
one control plane for many hosts.

## Risks / hard truths

- **Nested may stay impossible** on this hardware; treat it as a documented
  boundary, keep the reproduction, revisit only when the platform changes.
- **The uplink is slow (0.44 MB/s);** the entire storage design must stay
  delta-based. Never ship a full-disk upload.
- **The 2-active-VM cap** is a hard per-host ceiling; scale means more hosts.
- **Rectangles are big:** macOS disks are 30 GiB; the compact story is Linux
  (~10 MB) and shared bases, not shrinking macOS itself.

## Definition of done (for the whole plan)

A person can point a browser at a URL, see several machines on one or more Macs —
macOS and Linux — start, drive, snapshot, roll back and clone any of them, with
each additional machine costing kilobytes-to-megabytes rather than gigabytes, and
an AI agent completing a real task inside one of them without a single host
permission prompt. Every claim above is backed by a command in this repo that
prints the number.
