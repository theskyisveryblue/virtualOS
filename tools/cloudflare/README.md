# Cloudflare Stack for virtualOS

Two layers: a **Tunnel** for direct streaming, and a **Worker relay** for command-and-control from anywhere.

The Worker is deployed in the Ai account at
`https://virtualos-relay.ai-c24.workers.dev`. The browser console is
`https://virtualos-relay.ai-c24.workers.dev/console`.

The checked-in Wrangler binding is `SNAPSHOTS → virtualos-remote-private` in the
Ai account, alongside the `CONTROL` Durable Object and its SQLite migration.
The generated project briefing's `virtualos-snapshots` name is stale: the
[account cleanup receipt](../experiments/cloudscope-account-cleanup-receipt.json)
records removal of that bucket from the former account on October 12. Use
[wrangler.toml](worker/wrangler.toml) for the current deployment binding. This
source review is not a fresh cloud-account inventory.

On 2026-10-03, the Mini
poller returned a guest JPEG through the relay; the frame result was consumed
once and was not stored in R2.

## Architecture

```
 iOS / browser / curl
        │
        ▼
 ┌─────────────────────────┐
 │  Cloudflare Worker       │  ← transient command queue; optional R2 snapshots
 │  (Durable Object + R2)  │
 └────────┬────────────────┘
          │  long-poll
          ▼
 ┌─────────────────────────┐
 │  fleet_push.py (Mini)   │  ← guest frames and per-VM bounded input
 │  supervisor + guests    │
 └─────────────────────────┘
```

## Quick start

### 1) Deploy the Worker

```bash
./tools/cloudflare/poller-session.sh deploy-cloudscope
```

Use the `ai@damidina.com` Cloudflare account (`c24a3fb4bedba669785c18c834f51921`).
This reads the existing Ai connection from CloudScope's Keychain vault into
process memory without printing or copying its token to a file. The deployment
script checks `wrangler whoami`, creates the dedicated private
R2 bucket if absent, deploys the Worker, issues separate host and browser
secrets, copies the poller to the Mini, and restarts it. It refuses another
account's login before making changes. The browser key is saved with mode
0600 at `~/.config/virtualos/browser-token` on the operator Mac.

The deployment URL is `https://virtualos-relay.ai-c24.workers.dev`.

### 2) Check the Mini poller

```bash
ssh mac-mini-ai '/Users/ai/Documents/GitHub/virtualOS/tools/cloudflare/poller-session.sh status'
```

The deployment script starts the configured host transport on the Mini. The
current fleet path uses `fleet_push.py --token-file`; it streams guest frames
and executes per-VM input locally.

Open `/console` on the Worker URL for browser control of the guest desktop.
The browser secret is kept in the current tab only. Single-guest `frame` command
results are transient. The fleet stream stores the latest frame for each VM in
private R2 at `fleet/frames/<vm>.jpg`, overwriting that same key on each upload.
Frame responses prohibit browser caching; this does not remove the R2 object.
Browser control sends guest frames, clicks, keys and bounded text. The current **Wake
guest** button sends a single Space key; it does not submit a password. Guarded
unlock is a separate Mini-local operation that verifies the visible password
prompt before reading the private credential.

### 3) Use the browser console

Open the pinned `/console` URL and enter the narrower browser key saved at
`~/.config/virtualos/browser-token`. Keep that file private (mode 0600). The tab
keeps the key only in memory; disconnect clears the visible frame and typed
text. Select a machine and wait for a fresh frame before sending input. Stale
heartbeats disable control.

Do not copy bearer tokens into command arguments or send a guest password
through the relay. For a read-only fleet status check, the protected local
reader uses the existing token file without shell-evaluating its contents:

```bash
python3 tools/fleet/relay_read.py --token-file "$HOME/.config/virtualos/browser-token"
```

The host credential can perform additional administrative operations. Keep it
separate from the browser key; normal browser use does not need it.

### 4) R2 snapshots

The authenticated `/snapshots` routes list, upload, retrieve and delete explicit
snapshot objects. These routes persist the supplied bytes. The fleet's latest
frames also persist in their separate R2 keys; single-guest command results are
transient. Use the chunked cloud-disk workflow
in [CLOUD_DISK.md](../CLOUD_DISK.md) for full VM disks, including hash verification
and recovery gates.

The legacy `poller.sh` still implements additional host commands. It is a
separate compatibility path with a broader command surface and is not the live
fleet stream process. Its source now uses protected-file relay credentials and
stdin request bodies, excludes passwords from training traces, and creates
private unique capture files. Thirteen isolated fixtures pass on the Mini's
Python 3.9, including transport bounds, secret-argument checks, deployment
packaging and failed-upload reporting. This migration has not been deployed;
it does not narrow the legacy host command surface. See
[`legacy-relay-security-fixtures-20261019.json`](../experiments/legacy-relay-security-fixtures-20261019.json).

## Command flow

1. Client POSTs to `/cmd` → gets back `{ "id": "uuid" }`
2. Poller long-polls `/cmd/poll` → receives the command
3. The selected host executes the bounded guest command locally
4. Poller POSTs result to `/cmd/{id}/result`
5. Client can check `/cmd/{id}` for the result

## Training traces

The relay accepts explicit structured usage events for later review. Normal
browser and host control does not automatically create a training history.
The latest fleet frame is stored in R2 as described above, independently of
training traces. The poller captures before/after training traces only when
`VOS_RELAY_TRAINING_TRACE=1` is deliberately set.

- `POST /usage`
  - Accepts a JSON event like `{ "source": "host", "kind": "manual_action", "action": "click", "metadata": { ... } }`
- `GET /usage/recent`
  - Returns the most recent in-memory events from the Durable Object

Explicit `/usage` events are persisted in R2 under `usage/YYYY-MM-DD/...json`
only if `ALLOW_USAGE_UPLOADS=1` is configured on the Worker. The default is
disabled. Reviewed datasets use `tools/computer-r2-sync.sh` instead.

## Private app and website playbooks (staged design)

Keep a human-readable, versioned source playbook per app version or HTTPS site
origin. `./tools/computer-dataset.sh --playbook-validate FILE` checks symbolic
goal, allowed action, independent verifier, rights, consent provenance, and
split group before any cloud export. Its canonical export contains no page
text, window title, URL path/query, field value, credential, screenshot, or
personal style. An app or site inventory does not itself grant training rights.

The cloud layout is **not deployed**. The proposed private layout is a Worker
behind Access, D1 rows for exact app/version/goal/episode/step queries, and
immutable SHA-addressed R2 objects for reviewed, compressed manifests and
evidence. Publish evidence objects first, verify their hashes, then publish a
manifest and add the D1 index row last. Keep the original readable source for
review; compress only the approved canonical export. Record R2 object bytes,
Class A/B operations, D1 rows read/written, and Workers AI Neurons before
choosing retention or batch size. The current relay's permissive
`/usage/artifacts/:name` route is not this reviewed playbook API. No D1 binding
is added to `wrangler.toml` until a real database and account are chosen, so
the existing relay remains deploy-ready. See [R2 pricing](https://developers.cloudflare.com/r2/pricing/),
[D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/), and
[Access service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/).

[Clef](https://developers.cloudflare.com/workers-ai/models/clef/) can later
score a fixed list of reviewed next-action choices or abstention from compact
AX/DOM symbols. Grounding and independent task-state verification stay in the
app workflow. Use held-out app/site groups before claiming any improvement;
storage and logging alone do not update model weights.

## Cloudflare Tunnel (direct streaming)

For low-latency frame streaming (bypassing the Worker relay), use a Cloudflare Tunnel directly to the streaming server. See `cloudflared-config.example.yml` and `run_private_stack.sh`.

`verify_private_remote.sh` retains the direct-tunnel verifier's environment
interface (`REMOTE_URL`, Access client ID/secret and `VOS_AUTH_TOKEN`). It now
uses bounded HTTPS requests without credential arguments, proxy inheritance or
redirects. It rejects an unhealthy JSON response before requesting the frame,
and writes successful output to a unique private directory and file. A
35-second absolute deadline bounds slow responses. This legacy verifier checks
a Codex window frame's JPEG envelope; it does not prove a guest task completed.
The [security receipt](../experiments/private-remote-verifier-security-20261019.json)
records local tests only; no real tunnel capture was performed by that check.

The Tunnel and Worker relay can run side by side:
- Tunnel → real-time frame streaming to the iOS/Mac remote apps
- Worker → command-and-control, screenshots, snapshots from anywhere

## Security

- `AUTH_TOKEN` is stored as a Wrangler secret (never in code)
- Control endpoints require Bearer auth; `/health` and the static `/console` shell are public
- `WEB_TOKEN` permits only guest frame and bounded input commands
- Fleet commands have separate per-VM queues; the compatibility path retains its legacy queue
- R2 bucket is private, only accessible through the Worker
