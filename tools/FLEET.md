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

The supervisor uses a **generic platform** for the fleet (uniform + only platform
that advertises nested virt) and turns nested on when the bundle's
`Parameters.txt` sets `nestedVirtualizationEnabled: true`.

Set it interactively:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/ "VirtualOSMachines/vm-a.bundle/Parameters.txt"
d = json.loads(p.read_text()); d["nestedVirtualizationEnabled"] = True
p.write_text(json.dumps(d, indent=2))
PY
```

A guest that boots a nested VM must run the guest-side `virtualOS.app`/fleet
agent and then boot its own bundle; the outer host does not need nested support
to run flat VMs — it only enables the inner hypervisor.

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

- Supervisor, manager, wrapper, self-test: **working locally** (all 6 stages pass).
- Per-instance frame capture: **implemented** (`GET /fleet/frame/<name>`), verified
  returning `503` when no VM is running; a real guest frame requires a bootable
  bundle and a GUI session.
- Nested virt: **supported and enabled on this host** (macOS 15.7.3, M-series).
- Multiplexer: **proxies frames** (`/fleet/frame/<vm>`) alongside status/control.
- Cloudflare fleet routes: **worker builds clean** (`wrangler deploy --dry-run`);
  not yet deployed. Push path: `tools/fleet/fleet_push.py --relay URL --token ...`.
- Web viewer: `tools/fleet/web/index.html` (single file, no build).
- Linux guest: `tools/fleet/LinuxGuest.swift` compiles; **no real Linux boot run**.

## Related understanding tools

All reachable through `computer-fleet.sh` as well as directly:

- `tools/bin-inspect.sh` (`computer-fleet.sh inspect`) — read-only arm64/Mach-O.
- `tools/fs-inspect.sh` (`computer-fleet.sh fs`) — read-only filesystem/image.
- `tools/container-inspect.sh` (`computer-fleet.sh containers`) — container survey.
- `tools/linux-guest.sh` (`computer-fleet.sh linux`) — experimental Linux guest.
