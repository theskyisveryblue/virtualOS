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

Guest-side verification is still open, and the blocker is input delivery, not
the swap guard or the bridge. On the Mini the guest repeatedly falls to a blank
(12,415-byte) frame or a lock screen. `computer-wake.sh` (space) and
`computer-unlock.sh` (configured password) both changed the frame and unlocked
the guest when re-run against the Mini's own control server, so single keys can
reach the guest. But typing into the guest Terminal does not register: `/vm/exec`
and repeated `/vm/key` calls all report `ok` with the expected counts, the
virtualOS window was confirmed frontmost, yet a `pwd` typed as individual keys
produced no text or output at the `~ %` prompt in OCR. Two concrete suspects:
`sendText` maps unmapped punctuation to keycode 0 (`a`) rather than rejecting it
(`VMCommandServer.swift:516-531`), and the synthetic events may not be consumed
when the guest's Terminal is not the focused in-guest window. Until input
delivery is reliable and the token is provisioned into the guest through a
trusted channel, the guest-side `/v1/route` call remains unverified. Host-side
and self-test requests are confirmed. The swap guard gates background workers,
not one manual guest exec.

If using SSH instead of direct bridge access, run the API on the Mini with its
default loopback address, then open a tunnel from the guest to the Mini:

```bash
ssh -N -L 8765:127.0.0.1:8765 MINI_USER@192.168.64.1
```

The guest then calls `http://127.0.0.1:8765/v1/route` with the same bearer
header. This requires SSH service and guest access to the Mini.
