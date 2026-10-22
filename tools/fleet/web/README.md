# virtualOS fleet — archived direct browser experiment

A single-file, dependency-free browser page that shows a whole experimental
virtualOS fleet: one card per VM, live JPEG frames, nested-virtualization
capability, and per-VM start/stop controls.

- **No build step.** Open `index.html` directly, or serve the directory with any
  static file server.
- **No external dependencies.** No CDN, no npm, no frameworks — vanilla HTML,
  CSS, and JavaScript only.
- **No WebAssembly.** See ["On 'web assembly'"](#on-web-assembly) below.

> **Unsupported with the current native HTTP boundary.** The mux and hardened
> supervisor reject browser Origin/fetch requests and expose no wildcard CORS.
> Serving this page locally does not grant it native control access. Use the
> authenticated Worker `/console` at the configured project relay for browser
> access; its host transport remains a separate native client. This page is
> retained as an experimental reference, with a broader legacy control surface.

## What it shows

- **Fleet status** — polls `GET /fleet/status` and renders one card per instance:
  VM name, state/`ready` badge, control port, and any error string.
- **Live frames** — for each *ready* VM, an
  `<img src="<base>/fleet/frame/<vm>?t=<timestamp>">` refreshed on an interval
  (default **1500 ms**, adjustable in the UI).
- **Controls** — a **start** button (`POST /fleet/start/<vm>`) and a **stop**
  button (`POST /fleet/stop/<vm>`) per VM.
- **Capability** — `GET /fleet/capability`, showing whether this host can expose
  a hypervisor to guests (nested virtualization, i.e. VM-in-VM) and why/why not.

## Safety notes

- Server-provided data is rendered with `textContent` / DOM `createElement` only.
  There is **no `innerHTML`** with server data anywhere in the page.
- There is **no `eval`**, no `Function` constructor, and no dynamic script
  injection.
- The page is read-mostly: the only state-changing calls are the explicit
  start/stop buttons.

## Compatibility

The old direct connections to ports 9300 (mux) and 9200 (supervisor) are
unsupported. Do not add permissive CORS or strip browser headers to restore
this page. Native CLI clients may still use `tools/fleet/fleet.py serve --port
9300` and its loopback routes. Browser users should use the authenticated
Worker `/console`, which does not require exposing these local endpoints.

### Endpoints the page uses

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`  | `/fleet/status` | fleet snapshot (instances, readiness, capability) |
| `GET`  | `/fleet/frame/<vm>?t=<ts>` | latest JPEG frame for a VM |
| `GET`  | `/fleet/capability` | nested-virtualization support |
| `POST` | `/fleet/start/<vm>` | start one VM |
| `POST` | `/fleet/stop/<vm>` | stop one VM |

The mux forwards native status/capability/frame requests, `POST /fleet/start`
(all), and `/fleet/start/<vm>` or `/fleet/stop/<vm>`. `serve.sh` can still display
the static reference page, but its printed
local supervisor URL does not provide browser access under the current policy.

## On "web assembly"

The manifesto line — *"web assemblies are computers too"* — is honored here by
treating **the browser runtime itself as the delivery surface**, not by shipping
a `.wasm` binary. This page is itself the idea in miniature: a real computer
surface (a fleet of virtual machines on a Mac) is reached entirely through the
web — a static HTML document plus `fetch` is enough to observe and control it.
The web is the runtime; the assembly is the page.

To be honest about scope: **no WebAssembly binaries or wasm toolchains are used
or required**, because none are installed in this environment. This is a
deliberate, documented choice, not an omission — the point being demonstrated is
that *web delivery* is the assembly surface, and plain DOM + HTTP is sufficient
to make the fleet reachable as web.

## Limitations

- Frames are only shown for VMs the supervisor reports as `ready`; a stopped VM
  shows a placeholder.
- Frame capture in the supervisor is still being built out, so a `ready` VM may
  return `503` until its capture backend produces frames; the card shows
  "frame not available yet" in that case.
- `POST /fleet/start` (all VMs) is not wired to a button; the UI is per-VM.
- No authentication. The fleet HTTP surface is local-only and unauthenticated;
  do not expose it to a network you do not trust.
- Verified with `curl` and static checks (no eval/innerHTML); not yet exercised
  in a full browser session in this environment.
