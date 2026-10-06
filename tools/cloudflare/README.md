# Cloudflare Stack for virtualOS

Two layers: a **Tunnel** for direct streaming, and a **Worker relay** for command-and-control from anywhere.

The Worker is deployed in the Ai account at
`https://virtualos-relay.ai-c24.workers.dev`. The browser console is
`https://virtualos-relay.ai-c24.workers.dev/console`. On 2026-10-03, the Mini
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
 │  poller.sh (your Mac)   │  ← executes commands via AppleScript + stream server
 │  virtualOS + VM running │
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

The deployment script starts the poller on the Mini. It long-polls the Worker
and executes guest commands locally.

Open `/console` on the Worker URL for browser control of the guest desktop.
The browser secret is kept in the current tab only. `frame` images and command
results pass through the relay and are not written to R2 by default. Browser
control is limited to guest frame, click, key, type, and an explicit guarded
unlock action. The unlock button uses the Mini's existing `computer-wake.sh`
and `computer-unlock.sh`, which checks the guest screen before sending the
configured password.

### 3) Send commands from anywhere

```bash
# Set these once
export RELAY=https://virtualos-relay.yourname.workers.dev
export TOKEN=your-secret

# Start the VM
curl -X POST $RELAY/cmd -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"start"}'

# Type the password
curl -X POST $RELAY/cmd -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"password","params":{"password":"YOUR_VM_PASSWORD"}}'

# Take a screenshot (uploaded to R2)
curl -X POST $RELAY/cmd -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"screenshot"}'

# View the screenshot
curl -o screen.jpg $RELAY/screenshot -H "Authorization: Bearer $TOKEN"

# Type text
curl -X POST $RELAY/cmd -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"type","params":{"text":"hello world"}}'

# Press a key
curl -X POST $RELAY/cmd -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"key","params":{"key":"enter"}}'

# Keyboard shortcut (Cmd+V)
curl -X POST $RELAY/cmd -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"shortcut","params":{"key":"v","modifiers":["command"]}}'

# Click at normalized coordinates
curl -X POST $RELAY/cmd -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"click","params":{"nx":0.5,"ny":0.5}}'

# Focus an app
curl -X POST $RELAY/cmd -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"focus","params":{"app":"Terminal"}}'

# Check VM status
curl $RELAY/status -H "Authorization: Bearer $TOKEN"

# Stop the VM
curl -X POST $RELAY/cmd -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action":"stop"}'
```

### 4) R2 Snapshots

Store and retrieve small, explicitly approved snapshots. This Worker route is
not suitable for a 30 GiB VM disk; a live VM still needs a local block device.

```bash
# Upload a snapshot
curl -X PUT $RELAY/snapshots/my-vm-backup.tar.gz \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @my-vm-backup.tar.gz

# List snapshots
curl $RELAY/snapshots -H "Authorization: Bearer $TOKEN"

# Download
curl -o backup.tar.gz $RELAY/snapshots/my-vm-backup.tar.gz \
  -H "Authorization: Bearer $TOKEN"

# Delete
curl -X DELETE $RELAY/snapshots/my-vm-backup.tar.gz \
  -H "Authorization: Bearer $TOKEN"
```

## Available commands

| Action | Params | What it does |
|--------|--------|--------------|
| `start` | — | Launch virtualOS and start the VM; the app unlocks after detecting the guest password prompt when configured |
| `stop` | — | Quit virtualOS |
| `restart` | — | Stop then start |
| `screenshot` | — | Capture screen, upload to R2 |
| `type` | `text` | Type text into VM |
| `key` | `key` (enter/escape/tab/delete/up/down/left/right/space) | Press a key |
| `shortcut` | `key`, `modifiers[]` (command/shift/option/control) | Keyboard shortcut |
| `click` | `nx`, `ny` (0.0-1.0) | Click at normalized coordinates |
| `focus` | `app` | Focus an app in the VM |
| `status` | — | Report VM state |
| `password` | `password` (required) | Type password + press Enter |

## Command flow

1. Client POSTs to `/cmd` → gets back `{ "id": "uuid" }`
2. Poller long-polls `/cmd/poll` → receives the command
3. Poller executes locally (AppleScript / streaming server / screencapture)
4. Poller POSTs result to `/cmd/{id}/result`
5. Client can check `/cmd/{id}` for the result

## Training traces

The relay accepts explicit structured usage events for later review. Normal
browser and host control does not automatically persist command history or
screen frames. The poller captures before/after training traces only when
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

The Tunnel and Worker relay can run side by side:
- Tunnel → real-time frame streaming to the iOS/Mac remote apps
- Worker → command-and-control, screenshots, snapshots from anywhere

## Security

- `AUTH_TOKEN` is stored as a Wrangler secret (never in code)
- Control endpoints require Bearer auth; `/health` and the static `/console` shell are public
- `WEB_TOKEN` permits only guest frame and bounded input commands
- The Durable Object is a singleton — one command queue per deployment
- R2 bucket is private, only accessible through the Worker
