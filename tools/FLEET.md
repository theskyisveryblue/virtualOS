# virtualOS Fleet — multi-VM box + nested virtualization (experimental)

Goal: turn one Mac into a **box that runs multiple virtual computers at once**,
streams them out through one pipe, and can be fronted by Cloudflare. Nested
virtualization lets a guest itself run VMs, so the box can host VM-inside-VM.

This is experimental. It does not change the shipping `virtualOS.app` UI or its
single-VM launch path.

**The shell tool is the primary interface.** `tools/computer-fleet.sh` drives
everything; this document explains it. Run `tools/computer-fleet.sh help` for the
full command list.

## Pieces

| Layer | File | Role |
| --- | --- | --- |
| Capability probe | `virtualOS/VM/VMConfiguration.swift` | `nestedVirtualizationCapability()` + enable on generic platform |
| Param | `virtualOS/VM/VMParameters.swift` | `nestedVirtualizationEnabled` (optional, backwards compatible) |
| Supervisor | `tools/fleet/FleetSupervisor.swift` | headless Swift: boots N VMs, one HTTP surface, frame capture |
| Manager | `tools/fleet/fleet.py` | CLI + multiplexer (status, control, frame proxy) |
| Push | `tools/fleet/fleet_push.py` | republish status + frames to the Cloudflare relay |
| Wrapper | `tools/computer-fleet.sh` | build / up / down / status |
| Runbook | `tools/fleet/runbook.sh` | executable record: doctor→clone→boot→stream |
| Self-test | `tools/fleet/self_test.sh` | end-to-end validation (6 stages) |
| Web view | `tools/fleet/web/` | single-file browser fleet viewer |
| Linux guest | `tools/fleet/LinuxGuest.swift` + `tools/linux-guest.sh` | experimental `VZLinuxBootLoader` path |
| Relay | `tools/cloudflare/worker/src/index.ts` | `/fleet/status` + `/fleet/frame/:vm` |

## Supervisor

`FleetSupervisor.swift` is a standalone binary. It reuses the standard VM bundle
layout (`Disk.img`, `HardwareModel`, `MachineIdentifier`, `AuxiliaryStorage`,
`Parameters.txt`) so existing bundles join a fleet unchanged.

```
tools/computer-fleet.sh build
tools/computer-fleet.sh up vm-a,vm-b      # supervisor + multiplexer
tools/computer-fleet.sh status
tools/computer-fleet.sh frame vm-a out.jpg
tools/computer-fleet.sh down
```

Equivalent supervisor invocation directly:

```
tools/fleet/fleet-supervisor --root ~/VirtualOSMachines \
    --names vm-a,vm-b --http-port 9200 --base-port 9100 --start
```

Per-VM control ports are `base-port + index` (9100, 9101, …) matching the
existing node-agent convention, so each VM is addressable the same way as the
shipping single VM.

HTTP surface (local only, `127.0.0.1`):

- `GET  /fleet/health`
- `GET  /fleet/status`         — all instances + readiness
- `GET  /fleet/capability`     — nested-virtualization support
- `GET  /fleet/frame/<name>`   — current guest frame as `image/jpeg`
- `POST /fleet/start`          — start all
- `POST /fleet/start/<name>`   — start one
- `POST /fleet/stop/<name>`    — stop one

Frames are captured from a headless (off-screen) `VZVirtualMachineView` attached
to each machine. The supervisor runs its HTTP server off the main thread and
keeps the main thread as the AppKit run loop, which is required for the guest
framebuffer to render without an on-screen window.

## Nested virtualization

On macOS 15+ Apple silicon (M3+/M4), `VZGenericPlatformConfiguration` can expose
a hypervisor to the guest:

- Probe: `VZGenericPlatformConfiguration.isNestedVirtualizationSupported`
- Enable: `platform.isNestedVirtualizationEnabled = true`

**But a macOS guest cannot use the generic platform.** macOS boots only through
`VZMacPlatformConfiguration` + `VZMacOSBootLoader`; nested virtualization exists
only on `VZGenericPlatformConfiguration`. The two are mutually exclusive today,
so a nested *macOS* guest is not possible. Nesting is reachable only with a
generic-platform guest (Linux), which has no macOS desktop.

Set it interactively:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/ "VirtualOSMachines/vm-a.bundle/Parameters.txt"
d = json.loads(p.read_text()); d["nestedVirtualizationEnabled"] = True
p.write_text(json.dumps(d, indent=2))
PY
```

**Capacity matters more than nesting for a fleet.** Apple caps the number of
concurrently *active* VMs per host. On the M4 Mac mini used here the observed cap
is two, including any VM already running; exceeding it fails with
`The maximum supported number of active virtual machines has been reached.`
Check with `tools/fleet/runbook.sh capacity`. A fleet is therefore many machines
*stored* and a bounded number *live*, and the box must schedule, not just spawn.

## Multiplexer + Cloudflare

`fleet.py serve` exposes one endpoint that fans out to the supervisor — the
"stream everything out of one pipe" surface:

```
tools/computer-fleet.sh up vm-a,vm-b
# mux -> http://127.0.0.1:9300/fleet/status
```

Cloudflare relay routes (host pushes with `AUTH_TOKEN`, browser reads with
`WEB_TOKEN`):

- `POST /fleet/status`   — host heartbeat of the whole box (Durable Object)
- `GET  /fleet/status`   — client reads latest fleet status + staleness
- `PUT  /fleet/frame/<vm>` — host uploads a VM frame → R2 (`fleet/frames/<vm>.jpg`)
- `GET  /fleet/frame/<vm>` — client fetches latest VM frame

## Self-test

```
tools/fleet/self_test.sh
```

Validates compile, capability probe, supervisor HTTP, multiplexer fan-out, and
per-VM start/stop.

## Status

Verified on the operator Mac and on the M4 Mac mini:

- Supervisor, manager, wrapper, self-test, runbook: **working** (6 stages pass).
- Real frame capture: **proven end-to-end** — `GET /fleet/frame/<vm>` returned a
  live 1500×900 macOS login-screen JPEG (~280 KB) from a fleet-supervised clone.
  Early frames are black for ~90–120 s; `runbook.sh desktop` polls until real.
- Entitlements: the virtualization entitlement survives an ad-hoc signature
  **only** in the GUI (Aqua) launchd session, so the supervisor runs under a user
  LaunchAgent (see `runbook.sh agent`), never `nohup` over SSH.
- Concurrent VMs: **capped at 2 active per host** (production VM occupies one),
  confirmed with `runbook.sh capacity`.
- Cloudflare fleet routes: **deployed and live** at
  `https://virtualos-relay.ai-c24.workers.dev` (`/fleet/status`,
  `/fleet/frame/<vm>`); a real status + frame push from the Mini was verified,
  with the fetched frame's SHA-256 matching the sent one.
- `fleet_push.py` sends a normal `User-Agent`; Cloudflare returns error 1010 to
  the default `python-urllib` agent.
- Web viewer: `tools/fleet/web/index.html` (single file, no build).
- Linux guest: `LinuxGuest.swift` compiles and `--nested` was added; a generic
  EFI guest **boots from an NBD-served disk** (all three backends verified — see
  `tools/CLOUD_DISK.md`). A full Linux rootfs boot is not yet run.

## Cloud disk (network-booted storage)

The storage plane lives beside the fleet: a content-addressed, SHA-pinned disk
served over NBD, with R2 as the remote store and a bounded local overlay/cache.
The executable interface is `tools/computer-space.sh`; `tools/CLOUD_DISK.md`
explains it. The real VZ client was verified booting from an NBD disk for the
`objects`, `raw`, and `r2-fake` backends, and a **full macOS guest** was booted
with **no local disk** via `tools/fleet/runbook.sh netboot` (48k reads / ~860 MB
over NBD, all writes overlay-only, source RAW untouched).

## Related understanding tools

All reachable through `computer-fleet.sh` as well as directly:

- `tools/bin-inspect.sh` (`computer-fleet.sh inspect`) — read-only arm64/Mach-O.
- `tools/fs-inspect.sh` (`computer-fleet.sh fs`) — read-only filesystem/image.
- `tools/container-inspect.sh` (`computer-fleet.sh containers`) — container survey.
- `tools/linux-guest.sh` (`computer-fleet.sh linux`) — experimental Linux guest.
