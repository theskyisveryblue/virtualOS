# Local model API

`computer-model-api.sh` serves the bundled compact models to the VM over HTTP.
Inference is offline. The API only returns advice; it does not perform guest actions.
The same reviewed artifacts are stored in private `fdotinc` Hugging Face model
repositories; the local copies total about 192 KiB and keep VM inference
independent of Hugging Face availability.

At start the server loads a compact binary bundle when one exists at
`~/Library/Application Support/virtualOS/model-api/models.bundle.br` (about
15 KiB versus 118 KiB of JSON text; see `WEIGHT_QUANT_EXPERIMENT.md`). Build it
on the Mini with `./tools/computer-model-api.sh --bundle-mini`, which needs the
`brotli` CLI. Without the bundle the server reads the plain JSON unchanged.

Generate a token on the host and start the server:

```bash
export VOS_MODEL_API_TOKEN="$(openssl rand -hex 32)"
./tools/computer-model-api.sh
```

The default listener is `127.0.0.1:8765`. On the Mini, `bridge100` is
`192.168.64.1` and the guest is `192.168.64.2`. Start the persistent bridge
service on the Mini with:

```bash
./tools/computer-model-api.sh --bundle-mini
./tools/computer-model-api.sh --start-mini
./tools/computer-model-api.sh --status-mini
./tools/computer-model-api.sh --self-test-mini
```

`--start-mini` checks the bridge address, creates a random token at
`~/Library/Application Support/virtualOS/model-api/token` with mode `0600`,
and runs the server in a detached `screen` session on `192.168.64.1:8765`.
Copy that token to the VM through a trusted channel. The command never prints
it. For manual binding to another private interface, use
`--host PRIVATE_IP --allow-remote` with `VOS_MODEL_API_TOKEN` set.
`--self-test-mini` sends synthetic inputs through every live model endpoint and
checks that an unauthenticated request is rejected; it does not retain images.

Only use a trusted local network for the HTTP listener. Keep the token out of
shell history and request URLs. Each request must include
`Authorization: Bearer $VOS_MODEL_API_TOKEN` and `Content-Type: application/json`.
The service has no cloud dependency and does not log request bodies or URLs.

| Method and path | JSON body | Result |
| --- | --- | --- |
| `GET /v1/health` | none | Model availability |
| `POST /v1/route` | `{"task":"Click the open tab"}` | Interface mode and confidence |
| `POST /v1/critique` | `{"text":"The primary button is hard to see..."}` | Estimated ratings from human-written critique text |
| `POST /v1/design/compare` | `{"left_image_base64":"...","right_image_base64":"..."}` | Relative alignment, overlap, and whitespace preferences |

The design endpoint accepts PNG or JPEG images, at most 10 MB each. Bodies are
limited to 24 MB. Route tasks are limited to 1,000 characters; critique text
is limited to 20,000 UTF-8 bytes. The server handles one request at a time,
closes each response connection, and times out stalled connections after 15
seconds. Design images are deleted after
inference. All responses use JSON. The design model compares variants within a
design family and does not provide an overall UX verdict.

From the VM, after supplying the same token securely:

```bash
curl --fail-with-body -sS -H "Authorization: Bearer $VOS_MODEL_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"Click the open tab"}' \
  http://192.168.64.1:8765/v1/route
```

```bash
curl --fail-with-body -sS -H "Authorization: Bearer $VOS_MODEL_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"The main action has weak contrast and is hard to find."}' \
  http://192.168.64.1:8765/v1/critique
```

Guest-side access is verified. From inside the VM, `curl` reached
`http://192.168.64.1:8765/v1/route` with the bearer token and returned
`{"choice":"dom_click","confidence":0.99973,"known_feature_ratio":0.7143,
"known_features":5,"status":"advice_only"}`. An unauthenticated call from the
guest returned HTTP 401, so both reachability and authorization hold across the
VM bridge.

Two useful details from that work:

- Direct `/vm/exec` typing is unreliable for punctuation: pasted `:` arrived as
  `;`, so a literal `http://host:port` command was corrupted. The reliable path
  is the guest hex bootstrap (`run_guest_hex_script`, lowercase hex plus
  `xxd -r -p`), which only uses unshifted keys and carries the token safely.
- Two app bugs blocked a headless/background start, both now fixed in
  `MainViewController`: `attemptAutoStartIfNeeded` did not populate the selected
  bundle/parameters before auto-start, and it did not open the sandbox scope for
  the VM directory, so `Parameters.txt` was unreadable and the start silently
  aborted. The installed app must keep the
  `com.apple.security.virtualization` entitlement; re-signing without it makes
  the VM refuse to start.

The Mini's guest is healthy and the model API self-test still returns HTTP 200
from route, critique, and design and HTTP 401 without a token. The swap guard
gates background workers, not one manual guest exec.

If using SSH instead of direct bridge access, run the API on the Mini with its
default loopback address, then open a tunnel from the guest to the Mini:

```bash
ssh -N -L 8765:127.0.0.1:8765 MINI_USER@192.168.64.1
```

The guest then calls `http://127.0.0.1:8765/v1/route` with the same bearer
header. This requires SSH service and guest access to the Mini.
