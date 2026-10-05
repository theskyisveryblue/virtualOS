# virtualOS fleet — browser web view (EXPERIMENTAL)

A single-file, dependency-free browser page that shows a whole experimental
virtualOS fleet: one card per VM, live JPEG frames, nested-virtualization
capability, and per-VM start/stop controls.

- **No build step.** Open `index.html` directly, or serve the directory with any
  static file server.
- **No external dependencies.** No CDN, no npm, no frameworks — vanilla HTML,
  CSS, and JavaScript only.
- **No WebAssembly.** See ["On 'web assembly'"](#on-web-assembly) below.

> **EXPERIMENTAL.** This drives the local fleet supervisor over HTTP, which can
> start and stop virtual machines. Run it only against a supervisor/mux you
> control, and treat the page as a lab instrument, not a product UI.

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

## Serve it

From this directory:

```bash
cd tools/fleet/web
python3 -m http.server 8080
```

Then open <http://127.0.0.1:8080/> in a browser.

On load, the page tries the URL query param first, then falls back to the page's
own origin:

```
http://127.0.0.1:8080/?supervisor=http://127.0.0.1:9300
```

You can also just type the supervisor/mux base URL into the text field and press
**connect**; the page writes it back into `?supervisor=` so the link is
shareable.

> Serving over plain HTTP from `127.0.0.1` is fine for local use. If you load the
> page from `file://`, most browsers block cross-origin `fetch`, so **serve it**
> (or point it at a same-origin supervisor).

## Point it at the right process

The page expects a base URL that answers the fleet routes. Two options:

1. **Multiplexer** (recommended) — one pipe for the whole fleet:

   ```bash
   tools/fleet/fleet.py serve --port 9300
   # base URL: http://127.0.0.1:9300
   ```

2. **Supervisor directly** — the headless Swift binary's own HTTP surface:

   ```bash
   tools/fleet/fleet-supervisor --root ~/VirtualOSMachines \
       --names vm-a,vm-b --http-port 9200 --base-port 9100
   # base URL: http://127.0.0.1:9200
   ```

Both expose CORS headers (`Access-Control-Allow-Origin: *`) for the frame and
status routes, so the page can be served from a different port than the fleet
surface.

### Endpoints the page uses

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`  | `/fleet/status` | fleet snapshot (instances, readiness, capability) |
| `GET`  | `/fleet/frame/<vm>?t=<ts>` | latest JPEG frame for a VM |
| `GET`  | `/fleet/capability` | nested-virtualization support |
| `POST` | `/fleet/start/<vm>` | start one VM |
| `POST` | `/fleet/stop/<vm>` | stop one VM |

Note on the mux: it currently forwards `GET /fleet/status` and
`GET /fleet/capability` and proxies frames, and it forwards `POST /fleet/start`
(all) and `/fleet/stop/<vm>`. If a route is missing from your mux build, point
the page at the supervisor directly.

## `serve.sh`

A convenience wrapper that serves this directory and prints the URLs:

```bash
tools/fleet/web/serve.sh                 # serves on :8080
tools/fleet/web/serve.sh 8099            # custom port
VOS_FLEET_MUX_URL=http://127.0.0.1:9300 tools/fleet/web/serve.sh
```

It prints the page URL and the suggested supervisor URL to paste into the page.

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
