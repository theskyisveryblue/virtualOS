# The virtualOS Manifesto

> One program. Many Macs. Every computer a stream.

We believe a computer is not a box you buy. It is a boundary you draw — and a
program that draws it.

A program that can place an operating system into a boundary does not need the
boundary to be one. The same program runs one computer on your laptop, ten on a
Mac mini, a hundred across a rack, and a computer inside each of those. The Macs
are where it runs. The program is what it *is*.

virtualOS is that program. One program, running many machines, on many Macs,
each machine a stream you can see and drive from anywhere.

## What we are building

**One program, many Macs.** The unit is not the machine; it is the program that
makes machines. Install it on one Mac and you have a computer. Install it on
ten and you have a fleet. Add a Mac and the fleet grows; remove one and the
others never notice. Capacity is a number you change, not a decision you regret.

**Many computers per Mac.** Not one VM on a desktop, but tens of guests on a
single host: each a full machine, each addressable, each disposable, each
rebuildable from a file. A Mac mini under a desk becomes a small data center.
A laptop becomes a lab.

**Every computer a stream.** The screen of any guest, byte for byte, out of one
pipe. Compute stays where the silicon is; the pixels travel. Control is a stream
too — clicks, keys, scrolls, the same interface a human uses, exposed to programs
and to people alike. If it can be seen, it can be sent. If it can be sent, it can
be shared.

**Computers inside computers.** Nested virtualization turns a guest into a host.
A machine that can run machines can be *any* machine — a test rig, a build farm,
a sandbox that spawns sandboxes. The boundary composes, so capability composes.
The program is the same program, one layer down.

**A cloud that is yours.** Cloudflare is the edge, not the owner. A Durable
Object is the control plane, R2 the durable disk, a tunnel the door. Compute and
storage stay on hardware you control; the reach is global. The cloud is a window
into your computers, not a landlord over them.

**Small models, on the metal.** Intelligence should not require a round trip. The
judgment a computer needs — route, critique, design — runs in a few megabytes,
bundled, offline, authenticated, beside the machines themselves. Every Mac in the
fleet carries it. No Mac waits on a network to think.

## What we believe

**The program outlives the machine.** Hardware is rented, borrowed, replaced.
A program that turns any Mac into any number of computers is the thing worth
keeping — portable across machines, and downward into the VMs it creates.

**Isolation is freedom.** A boundary that holds lets you take risks: the unknown
OS, the untrusted program, the experiment you would never run on your own machine.
Safety is not the opposite of capability; it is what makes capability usable.

**Local first, cloud when it earns it.** Bytes stay close until distance buys
something real. R2 is not a disk; it is an archive, a backup, a recovery. A
Worker is not a Mac; it is an edge. We use each for what it is good at.

**Reproducible beats clever.** A machine should be a file. A fleet should be a
manifest. If a computer cannot be snapshotted, restored, cloned, and booted again
byte-for-byte, it is not yet a computer we trust with real work.

**The interface is the API.** Anything a human can do through a screen, a program
should do through the same screen. No shadow channels into guests — stream what
is there, drive what is shown. The same stream reaches a person and a model.

**Experimental is a discipline.** We mark what is proven and what is not. A
prototype that passes a self-test is evidence, not a product. The production disk
is sacred; the clone is where we experiment.

## The terrain: the tools we actually use

We do not pretend the metal is simple. These are the real layers, and we name
them, break them, and understand them.

**Virtualization and containerization to do the work.** A virtual machine is a
whole computer — its own kernel, its own kernel boundary, its own devices — and
that is what makes nested fleets possible. A container is a process with a
drawn boundary — cheap, fast, dense. We use both, for what each is: containers
where density and speed win, VMs where isolation and a real OS boundary win, and
nested VMs where a guest must become a host. The program chooses the boundary
that fits the job.

**Filesystems as first-class citizens.** Machines are files. A disk image is not
an opaque blob but a structure to read: RAW and sparse RAW, APFS with its
containers and snapshots, the ASIF direction Apple is moving toward, and the
read-only bases plus copy-on-write overlays that let one verified image serve
many machines. We treat storage as a system: a base, an overlay, a checkpoint, a
content-addressed chunk. We know our filesystems and we own our bytes.

**Linux, not only macOS.** A general program runs general machines. Linux
guests — their own boot path, their own filesystem, their own tools — are part
of the range, not a special case. The same program that streams a macOS guest
streams a Linux one. Breadth of operating systems is breadth of possibility.

**Understand the binaries.** On Apple silicon everything is arm64: code,
containers, VM images, and the frameworks underneath. We do not fear the binary.
We read it — inspect arches and fat binaries, dump symbols, disassemble, trace
dependencies, inventory the private selectors of the frameworks we build on.
When the documented API ends, we learn what actually runs, and we label it
exactly that: *inspection*, not a supported contract.

**Web assemblies are computers too.** The web is not only a viewer; it is a
runtime. WebAssembly turns it into one that can carry real computation — the
same fleet, described so a browser can host a peer. The controller, the stream,
the many Macs — all reachable as web assemblies, so the fleet extends past the
Macs that host it.

## The challenges we have earned

These are the scars. Every one of them is a claim we paid for.

- **Nested virtualization is a threshold.** It requires the right silicon and OS
  (Apple M-series on macOS 15+), and the entitlement and policy must permit it.
  A guest becomes a host only when all three agree.
- **The host caps how many machines can be awake at once.** Apple limits the
  number of concurrently *active* VMs per Mac. On our M4 Mac mini the observed
  limit is two, including any VM already running — so a fleet is many machines
  *stored* and a smaller number *live*, scheduled onto the host. This is why the
  box must queue, not just spawn. Measured with `runbook.sh capacity`.
- **An entitlement is only as good as the session that holds it.** An ad-hoc
  signature carries `com.apple.security.virtualization`, but a process launched
  over plain SSH is not in the GUI (Aqua) launchd session and the framework
  ignores it. The same binary works when launched by a user LaunchAgent. The
  entitlement is real; the *context* decides.
- **A macOS guest and nested virtualization are mutually exclusive today.**
  macOS boots through `VZMacPlatformConfiguration` + `VZMacOSBootLoader`; nested
  virtualization exists only on `VZGenericPlatformConfiguration`, which has no
  macOS boot path. The boundary composes only when both ends agree.
- **Headless still needs a display.** A guest framebuffer does not render from
  nothing: a `VZVirtualMachineView` must be placed in a real (if off-screen)
  window, and the guest needs ~90–120 seconds to paint its desktop. Early frames
  are black; a capture must wait for substance, not just for a response.
- **Headless is not the same as invisible.** A sandboxed, out-of-container bundle
  cannot hold a programmatic bookmark the way a user-granted one can; a GUI-session
  launch and an SSH launch do not behave the same. The production app must sign
  without the sandbox to own a VM outside its container.
- **A live disk is sacred and slow to move.** The running RAW is not a backup
  target. You clone, you verify a hash, you record a receipt, and only then do you
  consider the copy real. APFS copy-on-write clones make this cheap and safe.
- **Object storage is not a disk.** R2 is an archive, not exhaustive random-access
  storage. A network boot over object storage is a research path — base image,
  bounded cache, durable overlay — and it is not done until it boots, restarts,
  survives an outage, and restores. We have now *booted* a real guest from real
  R2 over NBD: the bytes were fetched by signed, SHA-pinned reads; a cold chunk
  cost hundreds of milliseconds, then the cache answered in microseconds; every
  write stayed in the local overlay and nothing wrote back to R2. The mechanism
  is real; carrying a full macOS disk at that scale is the next step.
- **A machine can run with no local disk.** The scale step landed: a full macOS
  guest — `VZMacPlatformConfiguration` + `VZMacOSBootLoader`, a real desktop —
  booted with **no `Disk.img` at all**, reading its boot volume live over NBD
  (48k reads, ~860 MB) while every write went to a bounded `/private/tmp` overlay
  (3.6k writes, ~25 MB) and the source RAW was never touched. The disk is now a
  service, not a fixture of the machine.
- **A fleet stores one base, not N disks.** Clones of a machine share almost
  every block, so storing each machine's disk in the cloud is wasteful. Measured
  on two real 30 GiB macOS clones: they collapse to 7,282 unique 4 MiB chunks
  instead of 13,371 (~45% less), and the second machine differs from the base by
  just **0.69% — 223 MB**. Booting a machine from the shared base plus that tiny
  delta produced its real desktop. So the fleet keeps **one immutable base** and a
  per-machine delta, and every additional machine costs almost nothing.
  This lives in `tools/experiments/`, deliberately separate from the stable disk
  plane.
- **Measure the delta *and* prove it reconstructs.** A sharing estimate is a
  claim; a round trip is a receipt. `shared_base.py verify` streams the base and
  the machine and asserts that delta-over-base equals the machine block-for-block,
  without materialising a 30 GiB image. Against the production disk as base and
  the *used* fleet machine `vm-a` (it has been booted, logged in, and driven), the
  delta is **20,326 of 491,520 blocks — 1.33 GB, 4.14%** — and the verify reports
  `mismatches 0, stray_delta_blocks 0, round_trip_ok true`. Two honest numbers,
  not one: a *fresh* clone differs from its base by ~0.7%, but a machine that has
  lived accumulates real changes (~4%). The method is the same; the freshness is
  the variable. `verify` is now a check in the fleet self-test.
- **Linux, not only macOS.** The box now boots a real Linux guest, not just macOS
  clones. An Alpine aarch64 kernel + 8.8 MB initramfs runs on
  Virtualization.framework, hands off to Alpine Init, and reaches an interactive
  shell; driving the serial console returned
  `Linux (none) 6.6.134-0-virt #1-Alpine SMP PREEMPT_DYNAMIC ... aarch64`. Two
  earned traps: `VZVirtualMachine.start` must run on the machine's own dispatch
  queue (else a SIGTRAP dispatch assertion), and `VZLinuxBootLoader` needs an
  **uncompressed** kernel `Image` — a distro `vmlinuz` (compressed EFI-stub PE)
  starts the VM but prints nothing, so it must be extracted first. This tiny
  guest is the cheap substrate for many computers and for nested VMs (generic
  platform + `VZLinuxBootLoader` is the only nested-capable boot path).
- **A boot-only Linux guest is not yet a persistent one.** Booting to a shell is
  not the same as a machine that keeps anything. `linux-persist-lab.sh` tried the
  real thing: format the attached disk, lay down an Alpine rootfs, boot FROM the
  disk (`root=/dev/vda`), write a marker, reboot, and check it survived. The
  rootfs gets written and an init is installed, but the disk-rooted boot fails
  with `mount /dev/vda failed: No such device`: this Alpine initramfs ships only
  `squashfs` and `loop` as boot drivers and carries no `ext4`/`jbd2` module
  (the kernel has ext4 as a module; the initramfs does not package it). So the
  persistence gap is a specific, documented artifact problem — ship a matching
  module or install a self-hosting kernel — not a Virtualization.framework limit.
  The lab prints `persistent: false` and says exactly why, rather than claiming a
  persistent guest.
- **The nested-VM boundary, measured.** We tried VM-in-VM for real instead of
  trusting the API flag. On the M4 host,
  `VZGenericPlatformConfiguration.isNestedVirtualizationSupported` returns
  `true`, and an Alpine guest boots fine inside VZ. But when *that guest's own
  kernel* probes its KVM subsystem, it logs `kvm [1]: HYP mode not available` and
  there is no `/dev/kvm`; running QEMU inside the guest then trips a TCG
  assertion (`regime_is_user: code should not be reached`) instead of booting.
  So: the host *advertises* nested support, but does not yet hand EL2/HYP through
  to the guest kernel on this platform — the second layer is blocked by Apple,
  not by our code. `tools/nested-lab.sh` reproduces this boundary end to end.
- **The cloud path outlives the LAN.** When the operator Mac left the Mini's
  LAN mid-session (subnet change, ping and SSH both dead), the fleet never went
  dark: the host kept pushing heartbeats to the Cloudflare relay and the relay
  kept the fleet status and liveness fresh. So the scoreboard must be
  relay-first — read the fleet from the relay and treat SSH as an optional
  enhancement — or a roaming operator sees a false "DOWN". `plan-status.sh` now
  does exactly that, and says "not reachable from here (Mini off-LAN)" for the
  data that genuinely lives on the host.
- **A heartbeat is not a control channel.** The host's push loop and its control
  loop share one process (push on the main thread, control on a daemon). We hit
  the exact failure this invites: the control thread died while the heartbeat
  kept flowing, so browser input hung as `pending` forever though the fleet
  looked perfectly live. Two fixes, both earned against the live stack: the host
  now guards the control loop (catch everything, exponential backoff, and
  resurrect the thread if it ever dies) and the relay expires a never-completed
  input after 60s with an explicit `expired` result. Proven end to end: with the
  operator off the Mini's LAN and holding only the browser token, `move`, `click`,
  `key`, and `type` all round-trip through the relay to the guest and return
  `{ok:true,status:200}`.
- **A browser can drive a guest with no host permission.** A remote client posts
  input to the relay (`POST /fleet/input/<vm>`), the host long-polls it
  (`GET /fleet/poll`), and the supervisor applies it to the guest's own
  keyboard/mouse through a hidden `VZVirtualMachineView` — so the guest OS sees a
  real user, not an app asking for Accessibility or Screen Recording. Verified
  live: `move`, `click`, `key`, and `type` all round-trip browser → relay →
  host → guest and return `{ok:true,status:200}`, and a click wakes a sleeping
  guest display (frame goes 22 KB → 278 KB). Two real traps were earned here:
  the supervisor's single `read()` could return headers-only or a partial body,
  so a JSON POST needs a read loop bounded by `Content-Length`; and typing must
  be paced on the main run loop (`asyncAfter`) rather than one `main.sync` plus a
  sleep per character, or a long string stalls the request past its timeout.
- **A local model can use that same input, offline.** `tools/fleet/brain.py` runs
  one small model per VM against the supervisor: it reads captured guest frames,
  asks a local model for one honest action, applies it through the per-VM input
  queue, and records a trace. `brain.py doctor` reports supervisor, model, and
  OCR readiness with `cloud_required=false`, and a shadow/act run completes with
  no host prompts and no network. The model is allowed to abstain — and does,
  which is the honest result, not a failure.
- **Remote-backed disks must survive failure.** A temporary network loss must
  reconnect against the same overlay without losing or corrupting writes. A
  network disk without a reconnect story is a toy.
- **Small models must be small.** Compression is a real engineering constraint —
  quantized weights, packed bundles, checked ratios — so inference runs on the
  metal, next to the machines, with no round trip.
- **Our knowledge must be reversible.** Every private API, binary, or protocol we
  lean on is paired with a way to inspect it and a note that it is unsupported.
  What we understand, we can rebuild; what we cannot inspect, we do not depend on.
- **A test must never break the machine it protects.** Our fleet self-test built
  the supervisor to the *production* path, overwriting the signed, entitled
  binary with an unsigned one. The running process kept its entitlement, so
  nothing failed until the next restart would have — and `runbook doctor` went
  from `virtualization present` to `MISSING` after a test run. A self-test builds
  to a scratch path, always. Found live, fixed, and re-signed.
- **If you cannot deploy it, run it where it is.** The relay's input-expiry fix
  was blocked from deploy (invalid Cloudflare token, no egress) — but
  `wrangler dev --local` runs the Worker in miniflare with no network, so the
  whole input lifecycle (auth roles, queue → poll → result, TTL expiry) is
  asserted offline. A change you cannot ship you can still prove. Testing it also
  surfaced that miniflare persists Durable Object state across restarts, so the
  test drains the restored queue first — that durability is the feature, not a bug.

## How we work

- **Prove it or label it.** Every claim is verified by a test, a measurement, or
  a receipt — or it is explicitly a hypothesis.
- **Never break the running machine.** Back up before you touch the disk. Clone
  before you boot the copy. Keep the RAW until the remote boot survives.
- **One pipe, many machines.** One status surface, one stream, so the many behave
  like one program.
- **Bound the blast radius.** Local sockets stay local. Authentication is a
  boundary, not a feature. Secrets never enter the repository.
- **Choose the right boundary.** Container, VM, or nested VM — density, isolation,
  or recursion. The tool follows the job; the job does not bend to the tool.
- **Leave a trail.** Docs state what ran, what passed, and what remains unknown.

## The end state

The same program runs on your laptop, on a Mac mini, on a rack of them. Each Mac
holds a fleet of full computers; each computer can hold more. Each is a macOS or
a Linux machine, or a container beside them, booting from a verified image, from
object storage, or from another computer. Every one of them is a stream — and
every stream can be hosted as a web assembly — watchable and drivable from
anywhere through one protected door. A few small models sit beside them, giving
them judgment without a network. Under all of it, we understand the arm64
binaries and the filesystems that make the layer real.

One program. Many Macs. Every computer a stream — and every stream a computer you
can reach.

We are not virtualizing to escape the machine. We are virtualizing to *multiply*
it. The computer was never the box. It was always the idea of one — and now it is
a program.
