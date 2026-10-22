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
- **Persistent Linux baseline.** An isolated Alpine 6.6.142 guest booted twice
  from its ext4 disk; the persisted marker grew from one entry to two. Root
  password login was locked, and the successful private disk and logs remain
  retained (`tools/experiments/linux-persistence-receipt-20261018.json`).
- **Maintained persistent Linux.** Signature-verified Alpine 3.24.2 userland and
  kernel 6.18.57 passed two isolated disk-rooted boots, with a locked root
  password and marker counts one then two. The 42-package installer retains no
  legacy regular userland files; existing guests stayed running
  (`tools/experiments/linux-maintained-persistence-receipt-20261018.json`).
- **Remote, live, multi-host.** The fleet streams to a Cloudflare worker; a browser
  view shows status, live frames, and start/stop; heartbeats are verified fresh.
- **Self-healing live stack.** One idempotent command (`runbook.sh live`) converges
  supervisor + VM + stream loop + relay, and a watchdog LaunchAgent converges it
  automatically when the relay heartbeat goes stale (proven by breaking the stack
  and watching it heal).
- **Guest input works.** Single-guest `/console` drives the guest's own
  keyboard/mouse and returns live frames.
- **A browser drives any VM.** The relay carries per-VM `move`/`click`/`key`/`type`
  to the host poller and the result back; proven live from an operator holding
  only the browser token, off the host's LAN.

## What is not true yet (the honest gap)

- **Nested VMs.** The host advertises `isNestedVirtualizationSupported = true`,
  but the guest kernel logs `kvm [1]: HYP mode not available` — **no EL2/HYP is
  handed through**. VM-in-VM is blocked by the platform today, not by our code.
- **Windows.** No Windows path (VMPal uses its own engine; we have not built one).
- **Concurrency.** One host caps at 2 active VMs; we queue but do not yet *scale
  across Macs*.
- **Speed of storage.** Full-disk cloud boot over the measured **0.44 MB/s** link
  is ~15 h — cloud boot must go through deltas, never brute force.
- **Linux rollout.** The maintained Alpine 3.24 proof is isolated; the default
  Linux guest and artifact selection still use their prior baseline. A
  reviewed migration and ongoing security update process remain open.

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

On October 20, new-machine `go-live.sh` provisioning was changed to require a
shared-disk configuration. Full local production clones now require explicit
`VOS_ALLOW_LOCAL_CLONE=1`. Seventeen scratch regressions passed on the operator
and Mini; the installed Mini path separately refused an implicit clone and
prepared a diskless fixture bundle. Its shared-storage dependencies are retained
in a private pinned package, leaving active storage processes unchanged.
This prevents new full-disk clones; the two existing running local disks have
not yet been migrated or retired. See
`tools/experiments/compact-provisioning-verification-20261020.json`.
Subsequent overlay writes now discard blocks restored to known base bytes,
with net capacity accounting and no extra remote base fetch for full writes.
The physical SQLite reclamation experiment uses a disposable fixture; this is
not evidence that an existing running VM disk has been shrunk.
The current two-machine COW scan found a 2.26 GB raw difference, with 1.17 GB
of compressed block payload before file overhead. Because that raw seed exceeds
the existing 2 GiB overlay budget, a new compressed read-only initial-delta
backend now keeps the seed below a separate writable overlay. Forty-three Mini
regressions and the full disk-plane self-test passed, including NBD reconnect
and service restart. Existing machine migration and guest boot proof remain open.

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
- **Already:** macOS booted; isolated Linux ext4 persistence proved through two
  boots with locked root login, including maintained Alpine 3.24.2 userland and
  Linux 6.18.57. Private proof disks and measured receipts are retained.
- **Next:** migrate the default Linux path to the verified maintained profile;
  try nested
  again *properly* (documented reproduction, try configs,
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

The current continuation uses two no-argument commands:

```sh
./tools/cloud-disk-observe.sh
./tools/cloud-disk-archive.sh
```

The worker supervises archive conversion, guarded Linux testing and cloud
recovery for 24 hours, reconnecting lost controllers without stopping live
guests. Existing jobs continue after that supervision window. Private state and
completed observer snapshots live in `/private/tmp/virtualos-archive-conversion`.
The observer reports operator disk headroom, Mini memory and model residency,
and dated security findings; it does not run inference or change host settings.
The worker's controller is implemented inside that same `.sh` file. Its private
queue keeps one active work lease, expires stale work, skips blocked tasks and
requires observer-attested receipt and source hashes before completion. The
observer reads that state without renewing work. The background supervisor
loads the same worker source afresh each tick without extending an agent lease;
subsequent improvements update these existing files in place. Thirty-two
controller and integration fixtures pass on Python 3.9.6. This coordinates the
existing jobs and the agent's next task; it does not launch extra model sessions.
On October 18, an existing Ai R2 account token recovered source/destination
access after its cached token ID became stale. The operator now verifies the
current account-token identity and derives S3 credentials only in memory.
One-hour source capabilities renew only for the pinned archive; S3 keys stay on
the operator. Restarted conversion reads and verifies existing chunks before
sending another PUT. A bounded new immutable write/readback passed against real
R2 (`tools/experiments/r2-operator-write-check-20261018.json`). The conversion
completed on October 19 with all 7,680 chunks verified and both
full stream hashes matching their pins. The immutable manifest is published
(`tools/experiments/r2-archive-base-conversion-20261019.json`). Independent full
R2 base readback completed at 04:36 UTC: the full 32,212,254,720-byte raw disk
matches its pin, with an independently checked private Mini receipt
(`tools/experiments/r2-independent-base-readback-20261019.json`). Recovery boots
remain pending because both macOS slots are occupied; no running disk is retired.

**P0 — Stay live (now).** Keep the current stack converged; the fleet watchdog
(`tools/fleet/fleet-watchdog.sh`, installed as `com.virtualos.fleet-watchdog`)
converges supervisor + VM + stream agent + heartbeat whenever the relay goes
stale. *Exit met:* `plan-status.sh` shows live + heartbeats fresh, and the
watchdog was proven to heal a deliberately broken stack.
The October 18 security update moved relay health checks into a bounded Python
reader, removing bearer tokens from `curl` arguments and avoiding shell execution
of relay settings. The watchdog now checks resource admission before supervisor
recovery or VM starts, and its action counter uses private locked JSON state.
Only the watchdog was reloaded; production, fleet supervisor and Linux PIDs
remained unchanged. The signed keyboard supervisor still awaits activation.
The October 19 session-wrapper migration also removed shell evaluation and
credential arguments, preserved unrelated deployment functions, and passed a
live status check without restarting services
(`tools/experiments/poller-session-security-migration-20261019.json`).
A separate native HTTP candidate now refuses browser origins, unexpected Host
headers and ambiguous framing, bounds concurrent clients and request time, and
survives abandoned connections. Ten real TCP checks, the full fleet self-test,
and the unsigned app build pass; these native changes are not active yet.
A private full app candidate also passes deep, strict signature verification
with the hardened runtime flag and the current app's four entitlements. It is
an ad hoc signed Debug candidate, has not been launched, and does not enable
App Sandbox (`tools/experiments/native-app-maintenance-candidate-20261019.json`).
Its private Mini copy matches every bundle entry and passes strict signature
verification (`tools/experiments/native-app-private-mini-stage-20261019.json`).
This policy accepts native clients and refuses the old dashboard's direct
browser requests. Browser control uses the authenticated Worker `/console`
through the native fleet stream; candidate guest usability is still unverified.
Fresh captures from both existing macOS guests were entirely black. Escape did
not wake vm-a, but the repository's documented single Space key did: a fresh
frame was visually confirmed at the password screen. No password was submitted
by that wake check; production was left untouched
(`tools/experiments/guest-space-display-wake-receipt-20261019.json`).
A later guarded unlock rechecked the visible password prompt before reading the
private credential and submitted it once. A fresh frame confirmed the desktop
(`tools/experiments/guarded-guest-unlock-20261019.json`). The small OCR model task
is still unverified. Its private backend now has tested network restrictions,
resource admission, and bounded cleanup of its own process group, including
startup failures. A private signed runtime resolved the pinned model manifest
and exited without loading weights
(`tools/experiments/private-mini-ollama-metadata-20261019.json`). Inference remains
deferred while available memory is below its 6 GiB admission requirement.

The host review also found FileVault off and the application firewall disabled,
with SIP and Gatekeeper enabled. Project native ports were loopback-only.
Host-wide hardening needs an SSH-preserving maintenance change and a verified
FileVault recovery plan; no firewall setting or reboot was changed by this review
(`tools/experiments/mini-host-security-review-20261019.json`).

**P1 — Compact fleet.** Make shared base+delta the default disk; boot machine B
from base+delta in the normal path; store deltas in R2; measure bytes/machine.
*Progress:* the delta is measured and **proven** to reconstruct a machine
byte-for-byte (`shared_base.py verify`, receipt committed), and the disk plane now
serves a machine as **base + delta over NBD** (`--nbd-base-delta-serve`,
verified by a real NBD client in the fleet self-test). *Remaining:* roll the
managed configuration out to the main fleet. The
`fleet/<machine>` R2 delta upload/pull path passes the local signed-HTTP test;
real R2 storage is now verified for a bounded synthetic three-block delta
(`tools/experiments/r2-delta-probe-receipt.json`). The real 25,404-block machine
delta now uploads and recovers exactly from the Ai account's R2 bucket. The
downloaded delta plus the Mini's shared base reconstructs all 491,520 disk blocks
with zero mismatches. Two recovered guest boots reached the login screen,
including a disk-service restart that reseeded zero blocks; both frames were
visually checked. Its R2 inventory matches the manifest exactly: 25,376 unique
block objects plus the manifest occupy **824,983,105 bytes**. Those boots used
a local shared base; the completed test disks were subsequently retired to
cold storage to reclaim space. Restarting that test requires restoring the
archived base and pinned delta. The two running VM disks remain local; their
cloud-backed migration is still outstanding.
Receipts are `tools/experiments/r2-machine-delta-receipt.json`,
`tools/experiments/r2-recovered-boot-receipt.json`,
`tools/experiments/r2-recovered-verify-receipt.json`, and
`tools/experiments/r2-machine-storage-receipt.json`.
The separate full-backup job also completed its full-download SHA-256 audit:
the 22,266,108,635-byte archive and its published receipt are present in the Ai
account's `virtualos-vm-backups` bucket. Its live size matches the audit receipt
(`tools/experiments/r2-vm-archive-receipt.json`); a guest boot from that full
archive remains unverified.
The October 13 cleanup removed both full reference disks, both duplicate seed
databases, and both completed-test overlays. The test fleet now reports about
43 MiB instead of 57 GiB. Actual host free space increased by **7,570,083,840
bytes**; APFS shared extents make that smaller than the sum of reported bundle
sizes. Production and `vm-a` stayed running. Cloud machine metadata prevents
`go-live.sh` from silently making another local clone for a cold machine.
Evidence: `tools/experiments/space-optimization-cleanup-receipt.json` and
`tools/experiments/r2-cold-machine-catalog.json`.
The October 13 guarded Swift cache purge reclaimed another **1,469,546,496
bytes** on the Mini, leaving 23 GiB free at that check. Both VM processes
remained running (`tools/experiments/mac-mini-cache-cleanup-20261013.json`).
The cloud-disk service now enforces its persistent chunk-cache budget at
startup, including after a budget reduction; the storage self-test covers
oldest-first eviction, a zero budget, and preservation of unrelated entries.
This startup change also passed the full storage self-test on the Mini after
deployment to its cold-test tools. Off-site SSH was recovered through the
existing protected credential-file configuration for `mac-mini.oto.house`;
the default SSH alias still refers to an obsolete hostname. The access
diagnostic now checks the working emergency route first. Neither running disk
was migrated and no VM was restarted.
On October 18, the Cloudflare SSH transport became unavailable while the Mini
rebooted independently. The operator reconnected through the known LAN address
using the same protected identity and pinned host key. Production and `vm-a`
were healthy afterward, with 23 GiB free. The detached archive worker's RAM
state was lost in that host reboot; the replacement keeps conversion generation
4 and verifies existing R2 chunks before reusing them. The independent browser
relay health check is also healthy. The archive conversion subsequently
completed on October 19; the independent recovery-boot proof remains pending.
The streaming archive converter
`cloud_disk_archive.py` can convert the existing pinned backup into verified
base chunks without extracting a local disk. A live Mini-to-Ai-R2 range probe
found GNU sparse 1.0 metadata that Python 3.9.6 interprets incorrectly; the
converter corrects its logical size and physical next-header offset. Sparse
reconstruction and following-member regression fixtures pass locally and on
the Mini. Evidence is `tools/experiments/r2-sparse-archive-header-receipt.json`.
The full conversion now matches both pinned stream hashes and has published
the immutable manifest. Independent full R2 base readback also matches the raw
disk pin; guest boots from that converted base remain pending. The disposable recovery control
path now checks that its loopback listener belongs to its own child before
control requests. All 26 focused fixtures pass on the operator and the Mini;
this source update did not restart the running audit
(`tools/experiments/recovery-private-fixtures-20261019.json`). No running disk
can be retired yet.
Restart safety is now verified: seeding pins the initial delta and preserves
guest edits across complete NBD server restarts. A changed delta, an unpinned
populated overlay, and seed writes exceeding storage guards are refused.
`go-live.sh` now preserves existing diskless NBD bundles and checks the requested
machine's state before starting it. `--prepare-only` validates and registers a
bundle without starting services; isolated boot-path regression tests cover this.
`go-live.sh` now provisions a diskless bundle and manages its persistent NBD
LaunchAgent when `shared-disks/<machine>.json` is present. It waits for the
managed process's readiness and a real NBD greeting before requesting VM start.
The managed path now passes a real Mac mini boot and disk-service restart:
two macOS guest boots reached the login screen, restart reseeded zero blocks,
and both source disks stayed unchanged. The 32.21 GB machine reconstructs from
a 1.665 GB delta with zero mismatches across all 491,520 blocks. Its live overlay
occupied 1.695 GB and the machine bundle had no `Disk.img`. Evidence is in
`tools/experiments/managed-shared-boot-receipt.json` and
`tools/experiments/managed-shared-verify-receipt.json`. The existing fleet VM and
watchdog were restored; the production VM stayed running throughout.
The supervisor registers newly prepared machines without restarting other VMs,
and binds its display to the current VM instance after stop/start.
The NBD entry point now has a persistent mode with state under the fleet root,
an exclusive overlay lock, and continued service beyond eight connections.
Both bounded lab mode and persistent mode pass the socket/restart regression.

**P2 — Agent control.** Per-VM input on the relay + a small verb surface +
snapshot-as-undo; complete a scripted task inside a VM with no host prompts.
The browser-token-only relay path opened Documents in Finder, independently
confirmed in a fresh guest frame, without model inference or host permission
prompts (`tools/experiments/fleet-browser-documents-20261017.json`). On October
18 the human console was moved onto that active per-VM queue. An isolated
sandboxed Chrome session verified machine selection, a live 1500x900 frame,
cleared access-key input, no local/session storage, and private-view cleanup on
disconnect (`tools/experiments/fleet-console-live-20261018.json`). The console
refuses stale frames and text exceeding the guest's 512-character input bound.
The bounded checkpoint prototype now proves consistent snapshots with an active
SQLite connection, restoration of earlier bytes into a separate overlay, and
correct reads through two fresh NBD connections. The current overlay retains
its later changes; existing destinations and unsafe checkpoint input files are
refused (`tools/experiments/overlay-undo-boundary-20261018.json`). Applying undo
to a running guest and an independently verified small-model task remain open.
The optional OCR perception path in `brain.py` can use an installed small text
model: it selects an index from bounded detected text targets, sends no image,
requests two CPU threads and a 2,048-token context, and immediate model
unload afterward. Thirty-five focused brain tests and ten model-budget tests pass,
including refusal of invented coordinates. OCR admission also requires an
installed model no larger than 512 MiB, at least 6 GiB available memory, no
active swap-outs, and one VM/worker with a locked inference session. Chat requests
use the full checked manifest digest, with no mutable-tag fallback. Remote-routed
models are refused before guest text is sent. A changed or unavailable pre-action
frame also prevents input. Each OCR request rechecks host pressure and loaded
models; later refusals retain earlier action evidence. Input connection failures
and missing follow-up frames stop without replay or a completion claim. Guest
frames and health responses have explicit byte bounds, with JPEG pixel dimensions
checked before OCR. Trace-write failures retain action evidence and stop the run.
Live OCR
model inference and the independent guest-task proof are still unverified.
The operator's installed runtime refuses the full-digest reference and its disk
reserve prevents private staging there. On the Mini, a separate signed 0.40.6
runtime and checksum-verified 398 MB model passed the real metadata-only
full-reference check. Its private process group stopped and was reaped; no
inference, model download or global model-store write occurred
(`tools/experiments/private-mini-ollama-metadata-20261019.json`). The inference
backend passes eleven focused lifecycle and sandbox tests, but has not started
an inference session. Both global model residency and fresh host headroom remain
admission gates.

**P3 — Breadth.** Persistent Linux guest; honest nested re-attempt with a
documented reproduction matrix; Windows only if a real path appears.
The October 18 isolated legacy Linux proof passed both disk-rooted boots and
the locked-root check without restarting existing guests. Its 256 MiB logical
proof disk reports 15,589,376 allocated bytes and has a recorded SHA-256; that
allocation is not a measurement of unique APFS extents. The resource gate stays
in force, and failed disposable disks now have guarded bounded retention.
The separate maintained Alpine 3.24.2 proof also passed both disk-rooted boots,
using kernel 6.18.57 and 42 signature-verified MAIN packages. Its retained
256 MiB logical disk reports 22,974,464 allocated bytes, with logs and SHA-256
recorded in the maintained receipt. Default guest selection remains unchanged;
production rollout and nested virtualization remain open. A private maintained
selection now validates against a separate copy-on-write runtime disk with its
own inode and the sealed proof's SHA-256. Selection validation passed without
booting a guest or changing the default
(`tools/experiments/linux-maintained-selection-preparation-20261019.json`).

**P4 — Multi-host.** Scale past the 2-VM cap with a second Mac; fleet scheduler;
one control plane for many hosts.

## Small-model execution plan (2026-10-11)

| Gate | Current evidence | Next action |
| --- | --- | --- |
| Reproducible public models | The router, critique interpreter, and three design rankers retrained from checksum-pinned public data; all model and metrics files matched the local served copies byte for byte. | Keep `./tools/computer-train.sh --router`, `--critique`, and `--design` as the reproduction gate. |
| Durable guest recovery | The 22.27 GB full VM archive completed its full-download SHA-256 audit and has a published receipt in the Ai account. The real R2 machine delta recovered exactly and booted twice against a local shared base. Completed test disks were removed; the two running disks remain local. | Validate full-archive restore and full macOS boot against an R2 base before migrating a running disk. Keep cold tests diskless until a bounded restore is needed. |
| New private training evidence | The guest frame and OCR recovered after the VM restart and `inputctl` rebuild. The local feedback model has 39 verified outcomes across four app groups; grouped holdout is 6/11, equal to always abstaining. Mini resource pressure currently pauses guest tasks. | Wait for the resource guard, then collect independently verified actions across more app/site scopes, including failures and abstentions. Keep the model in shadow until validation and holdout beat the abstention baseline. |
| Offline serving | The Mini model API returns 200 for route, critique, and design requests and rejects an unauthorized request with 401. | Continue hourly health checks through the bounded `.sh` worker. |
| Hugging Face copy | The current token cannot read the three private `fdotinc` model repositories. Local models and source checksums remain available. | Re-run `./tools/computer-hf.sh --verify` when a token with access is available; do not claim remote parity before it passes. |

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
