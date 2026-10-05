# Containers beside the fleet — the other boundary

> *"Virtualization and containerization to do the work."*
> — [`MANIFESTO.md`](../MANIFESTO.md), *The terrain: the tools we actually use*

virtualOS is a program that draws boundaries around machines. The manifesto
names two boundaries and treats them as one discipline: a **virtual machine** is
a whole computer — its own kernel, its own kernel boundary, its own devices —
and a **container** is a process with a drawn boundary — cheap, fast, dense.
The program chooses the boundary that fits the job. This document explains the
two, the third (nested VMs), which runtimes `tools/container-inspect.sh`
detects, its read-only guarantee, and how containers complement the VM fleet.

## The two boundaries (and a third)

| Boundary | What it is | Cost | Density | Isolation | Where it lives here |
| --- | --- | --- | --- | --- | --- |
| **Container** | A process with a drawn boundary — namespaces, cgroups, a shared kernel. No separate kernel, no emulated devices. | Cheap: starts in milliseconds, megabytes of overhead. | High: tens to hundreds on one host. | Process-level. Strong against untrusted programs *if* the kernel and runtime are trusted; shares the host kernel. | `tools/container-inspect.sh` inspects the local runtime. |
| **VM** | A whole computer — its own kernel, its own kernel boundary, its own devices, run by Apple's `Virtualization.framework`. | Expensive: boots a kernel, reserves memory, attaches a disk. | Low: tens per Mac, not hundreds. | Full: a real OS boundary the guest cannot step outside of. | [`tools/fleet/FleetSupervisor.swift`](fleet/FleetSupervisor.swift), `virtualOS/VM/`. |
| **Nested VM** | A guest that *is itself a host* — a machine that can run machines. Turned on with `VZGenericPlatformConfiguration.isNestedVirtualizationEnabled` on macOS 15+ Apple silicon. | Most expensive. | Lowest. | Full, and recursive: the boundary composes. | `tools/FLEET.md`, `nestedVirtualizationCapability()`. |

The manifesto is explicit about why both exist:

> containers where density and speed win, VMs where isolation and a real OS
> boundary win, and nested VMs where a guest must become a host.
> The program chooses the boundary that fits the job.

A container is not a small VM, and this project does not pretend it is. It is
the cheap, dense, process-level boundary that runs beside the fleet.

### On macOS specifically

Linux containers on a Mac cannot share the macOS kernel — the container needs a
Linux kernel. So every macOS container runtime is laid over a lightweight Linux
VM (Apple `container` uses a per-container VM built on `Virtualization.framework`;
`colima`/`podman machine`/Docker Desktop run a shared Linux VM). That makes the
layering legible: **the VM boundary carries the container boundary.** virtualOS
already owns the VM layer; containers are the dense tier that rides on it.

## What `container-inspect.sh` detects

The tool probes for five runtimes with `command -v` and reports, for each:

| Runtime | Version query | Service/daemon query (read-only) | List query (read-only) |
| --- | --- | --- | --- |
| `container` (Apple) | `container --version` | `container system status` | `container list` |
| `docker` | `docker version --format` | `docker info` | `docker ps --format` |
| `podman` | `podman --version` | `podman info` | `podman ps --format` |
| `nerdctl` | `nerdctl --version` | `nerdctl info` | `nerdctl ps --format` |
| `colima` | `colima version` | `colima status` | `colima list` |

- **Apple `container` specifics.** When the Apple CLI is present the tool also
  prints `container system status` and `container list` verbatim (indented), and
  **tolerates unknown or renamed subcommands gracefully** — a failure there is
  reported, not fatal.
- **Missing runtimes** get a clear `status : not installed` line and the tool
  continues. It never fails just because nothing is installed.
- **List output is summarized** to a count plus the first N entries (default 5,
  `--lines N`), never a full dump.

### Machine resources

- `sysctl -n hw.ncpu` — CPU count.
- `sysctl -n hw.memsize` — physical memory (raw bytes plus a human figure).
- `sysctl -n kern.hv_support` — whether the hypervisor (`Virtualization.framework`)
  is supported on this host.
- `sw_vers` — macOS name, version, and build.

### Usage

```sh
./tools/container-inspect.sh              # human-readable report
./tools/container-inspect.sh --json       # one machine-readable JSON object
./tools/container-inspect.sh --lines 10   # show up to 10 entries per runtime
./tools/container-inspect.sh --help
```

Exit codes: `0` success (including when *no* runtime is installed), `2` bad
usage.

## The read-only guarantee

`container-inspect.sh` is strictly **read-only by construction**. It issues only
`version`, `status`, `info`, `list`, and `ps` queries — the exact commands a
human would type to *look*. There is no code path that starts, stops, creates,
runs, pulls, removes, kills, or execs a container or a runtime service. It makes
**no network requests**. If a runtime is missing it says so and moves on. It
never mutates state, because inspection is not a supported contract — it is a
magnifying glass, exactly as the manifesto insists for everything we lean on:

> Every private API, binary, or protocol we lean on is paired with a way to
> inspect it and a note that it is unsupported.

## How containers complement the fleet

The manifesto's end state is *"each Mac holds a fleet of full computers"* and
*"a container beside them."* Containers and VMs are the two tiers of the same
idea — **capacity is a number you change**:

- **Density vs isolation.** Use a **container** for the many, small, fast,
  disposable jobs where process-level isolation is enough and you want hundreds
  per host. Use a **VM** for the job that needs its own kernel — the unknown OS,
  the untrusted program, the experiment you would never run on your own machine.
  Use a **nested VM** when a guest must become a host. Same program, three
  boundaries, chosen per job.
- **Containers ride the VM tier we already own.** On Apple silicon a Linux
  container is a process inside a lightweight Linux VM; the fleet's VM machinery
  is the substrate. Inspecting the local runtime tells you what the dense tier
  can do *right now* without disturbing the full-machine tier.
- **One status surface.** Like `fs-inspect.sh` for filesystems and
  `bin-inspect.sh` for binaries, this tool makes a boundary legible before we
  build on it — *"prove it or label it."*

## Relation to existing repo work

- [`MANIFESTO.md`](../MANIFESTO.md) — *"Virtualization and containerization to
  do the work"* and *"Choose the right boundary. Container, VM, or nested VM —
  density, isolation, or recursion."* This document makes that line concrete.
- [`tools/FLEET.md`](FLEET.md) — the VM/nested-VM tier: the supervisor, the
  capability probe, and nested virtualization. Containers are the tier beside it.
- [`tools/fs-inspect.sh`](FS_INSPECT.md) — read-only filesystem inspection; the
  same "inspect before you trust" discipline applied to disks.
- [`tools/bin-inspect.sh`](BIN_INSPECT.md) — read-only arm64/Mach-O inspection;
  the framing this tool borrows: read-only, understanding-only, no network.

## Limitations

- **It reports; it does not manage.** No lifecycle operations, by design.
- **Presence is not readiness.** A runtime can be installed and its daemon
  stopped (as `docker`/`colima` are on a default install); the tool reports both
  independently.
- **List summaries are capped.** Counts plus the first N entries, not the whole
  list.
- **Apple `container` is new.** Its command surface may change; unknown
  subcommands are tolerated rather than assumed.
- **No network.** It never contacts a registry, daemon socket remotely, or the
  internet.
