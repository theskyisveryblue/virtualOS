# virtualOS Stream Server (MVP)

Simple local HTTP server that exposes a JPEG frame endpoint.

## Run

```bash
cd /Users/theone/Documents/GitHub/virtualOS
python3 tools/virtualos_stream_server/server.py --host 127.0.0.1 --port 8899
```

To serve the LAN, set a token:

```bash
cd /Users/theone/Documents/GitHub/virtualOS
VOS_AUTH_TOKEN='replace-with-strong-token' \
python3 tools/virtualos_stream_server/server.py --host 0.0.0.0 --port 8899
```

Private-only (no LAN discovery):

```bash
cd /Users/theone/Documents/GitHub/virtualOS
VOS_AUTH_TOKEN='replace-with-strong-token' \
python3 tools/virtualos_stream_server/server.py --host 127.0.0.1 --port 8899 --no-bonjour
```

When bound to a LAN address, the server advertises itself via Bonjour as `_virtualosremote._tcp`.
Disable this with:

```bash
VOS_AUTH_TOKEN='replace-with-strong-token' \
python3 tools/virtualos_stream_server/server.py --host 0.0.0.0 --port 8899 --no-bonjour
```

Endpoints:
- `GET /health`
  - Includes `bonjourServiceType` for discovery clients
- `authRequired` is `true` when `VOS_AUTH_TOKEN` or `--auth-token` is set
- `GET /discover`
  - Returns host name + local IPv4 addresses + port for quick manual connect checks
- `GET /debug` (browser stream + control UI)
- `GET /frame.jpg`
  - Optional query: `?source=fullscreen` (or `?full=1`) to force full-screen capture
  - Optional query: `?source=window&app=Codex` to capture a specific app window
  - Optional query: `?max=1400` to downscale frame for mobile clients while preserving click mapping
- `POST /control/focus` `{ "app": "Codex" }`
- `POST /control/type` `{ "text": "hello" }`
- `POST /control/key` `{ "key": "enter" }`
- `POST /control/shortcut` `{ "key": "v", "modifiers": ["command"] }`
- `POST /control/click` `{ "x": 500, "y": 400 }`
  - Or normalized dynamic click: `{ "nx": 0.52, "ny": 0.81, "source": "window", "app": "Codex" }`
  - `source` can be `window` (virtualOS window-relative) or `screen` (full desktop-relative)

Auth headers supported when token is enabled:
- `Authorization: Bearer <token>`
- `X-VOS-Token: <token>`
- `X-Remote-Token: <token>`

## Smoke test

Run the full server sanity pass (health + sequential frames + concurrent frames + control):

```bash
cd /Users/theone/Documents/GitHub/virtualOS
tools/virtualos_stream_server/smoke_test.sh 127.0.0.1 8899
```

## Notes
- Guest VM frames from the app's local `/vm/screen` endpoint do not use macOS Screen Recording. The app no longer starts a host-screen capture server at launch.
- The separate host-window stream server may request **Screen Recording** permission for its launcher (for example, Terminal). Grant it only if you want host-window streaming.
- If window capture fails, server falls back to full-screen capture.
- For control endpoints, grant **Accessibility** permission to Terminal as well.
- If the iOS app says `Live` but image is black, the captured `virtualOS` window is currently black. Click/focus the running VM window in `virtualOS` first, then pull a new frame.
- For Cloudflare private routing, use a named tunnel + Cloudflare Access service token. Set `CF-Access-Client-Id` and `CF-Access-Client-Secret` in the iOS/Mac app.
- Full private Cloudflare setup is in `/Users/theone/Documents/GitHub/virtualOS/tools/cloudflare/README.md`.
