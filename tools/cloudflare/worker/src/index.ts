/**
 * virtualOS Cloudflare Worker — control plane relay
 *
 * Sits between remote clients (iOS/macOS app, curl, browser) and a Mac
 * running virtualOS at home. The Mac polls this Worker; clients push
 * commands to it. Durable Object keeps state per-machine.
 *
 * Routes:
 *   POST /cmd             — queue a command  (client → worker)
 *   GET  /cmd/poll        — long-poll for next command  (mac → worker)
 *   POST /cmd/:id/result  — post command result  (mac → worker)
 *   GET  /cmd/:id         — get command result  (client → worker)
 *   GET  /status          — latest machine heartbeat
 *   POST /status          — push heartbeat  (mac → worker)
 *   GET  /screenshot      — latest screenshot  (proxied from R2)
 *   PUT  /screenshot      — upload screenshot  (mac → worker → R2)
 *   POST /usage           — append a structured usage/training trace event
 *   GET  /usage/recent    — recent in-memory trace events for inspection
 *   PUT  /usage/artifacts/:name — upload a trace artifact to R2
 *   GET  /usage/artifacts/:name — fetch a trace artifact from R2
 *   GET  /snapshots       — list VM snapshots in R2
 *   PUT  /snapshots/:name — upload a snapshot to R2
 *   GET  /snapshots/:name — download a snapshot from R2
 *   DELETE /snapshots/:name — delete a snapshot from R2
 *   POST /fleet/status    — push fleet heartbeat (mac → worker → DO)
 *   GET  /fleet/status    — latest fleet status (client → worker)
 *   PUT  /fleet/frame/:vm  — upload a VM frame (mac → worker → R2)
 *   GET  /fleet/frame/:vm  — latest VM frame (client → worker → R2)
 *   POST /fleet/input/:vm  — queue input for a VM (client → worker → DO)
 *   GET  /fleet/poll       — long-poll for next fleet command  (mac → worker)
 *   POST /fleet/input/:vm/result/:id — post input result  (mac → worker)
 *   GET  /fleet/input/:vm/:id — read input result  (client → worker)
 *   GET  /health          — health check
 */

export interface Env {
  CONTROL: DurableObjectNamespace;
  SNAPSHOTS: R2Bucket;
  AUTH_TOKEN: string;
  WEB_TOKEN?: string;
  ALLOW_USAGE_UPLOADS?: string;
  INPUT_TTL_MS?: string;
}

interface UsageEvent {
  id: string;
  ts: string;
  source: string;
  kind: string;
  action?: string;
  commandId?: string;
  metadata?: unknown;
}

const REDACTED = "[REDACTED]";
const SENSITIVE_FIELD_PATTERN =
  /(?:^|_|-)(?:password|passphrase|secret|token|authorization|cookie|session|api(?:_|-)?key|text|cmd|query)$/i;

// ── auth helper ──────────────────────────────────────────────────────

function authorizationRole(request: Request, env: Env): "host" | "browser" | null {
  const header = request.headers.get("Authorization") ?? "";
  const xToken = request.headers.get("X-VOS-Token") ?? "";
  if (env.AUTH_TOKEN && (header === `Bearer ${env.AUTH_TOKEN}` || xToken === env.AUTH_TOKEN)) return "host";
  if (env.WEB_TOKEN && (header === `Bearer ${env.WEB_TOKEN}` || xToken === env.WEB_TOKEN)) return "browser";
  return null;
}

function unauthorized(): Response {
  return new Response(JSON.stringify({ ok: false, error: "unauthorized" }), {
    status: 401,
    headers: { "Content-Type": "application/json" },
  });
}

// ── main fetch ───────────────────────────────────────────────────────

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const id = env.CONTROL.idFromName("singleton");
    const stub = env.CONTROL.get(id);

    if (path === "/health") {
      // Health check is public but reveals no sensitive info
      return json({ ok: true });
    }

    if (path === "/console" && request.method === "GET") return browserConsole();

    const role = authorizationRole(request, env);
    if (!role) return unauthorized();
    if (role === "browser") {
      if (path === "/status" && request.method === "GET") return stub.fetch(request);
      if (/^\/cmd\/[0-9a-f-]{36}$/.test(path) && request.method === "GET") return stub.fetch(request);
      if (path === "/fleet/status" && request.method === "GET") return stub.fetch(request);
      if (path.startsWith("/fleet/frame/") && request.method === "GET") {
        const vm = decodeURIComponent(path.slice("/fleet/frame/".length));
        if (!/^[a-z0-9._-]{1,64}$/i.test(vm)) return json({ ok: false, error: "invalid vm name" }, 400);
        return downloadFleetFrame(env, vm);
      }
      if (/^\/fleet\/input\/[a-z0-9._-]{1,64}(\/[0-9a-f-]{36})?$/i.test(path)) {
        const body = request.method === "POST" ? await safeJson<FleetInputBody>(request.clone()) : null;
        if (request.method === "POST" && !allowedFleetInput(body)) {
          return json({ ok: false, error: "browser input refused" }, 403);
        }
        return stub.fetch(request);
      }
      if (path === "/cmd" && request.method === "POST") {
        const body = await safeJson<{ action?: string; params?: Record<string, unknown> }>(request.clone());
        if (!allowedBrowserCommand(body)) return json({ ok: false, error: "browser command refused" }, 403);
        return stub.fetch(request);
      }
      return json({ ok: false, error: "browser token cannot access this route" }, 403);
    }

    // ── snapshot routes (R2) ───────────────────────────────────────
    if (path === "/snapshots" && request.method === "GET") {
      return listSnapshots(env);
    }
    if (path.startsWith("/snapshots/")) {
      const name = decodeURIComponent(path.slice("/snapshots/".length));
      if (request.method === "PUT") return uploadSnapshot(env, name, request);
      if (request.method === "GET") return downloadSnapshot(env, name);
      if (request.method === "DELETE") return deleteSnapshot(env, name);
    }

    // ── screenshot shortcut (R2) ──────────────────────────────────
    if (path === "/screenshot" && request.method === "PUT") {
      return uploadScreenshot(env, request);
    }
    if (path === "/screenshot" && request.method === "GET") {
      return downloadScreenshot(env);
    }

    // ── fleet (multi-VM box) ──────────────────────────────────────
    if (path === "/fleet/status" && request.method === "POST") {
      return stub.fetch(request); // host heartbeat, stored in DO
    }
    if (path === "/fleet/status" && request.method === "GET") {
      return stub.fetch(request);
    }
    if (path.startsWith("/fleet/frame/")) {
      const vm = decodeURIComponent(path.slice("/fleet/frame/".length));
      if (!/^[a-z0-9._-]{1,64}$/i.test(vm)) {
        return json({ ok: false, error: "invalid vm name" }, 400);
      }
      if (request.method === "PUT") return uploadFleetFrame(env, vm, request);
      if (request.method === "GET") return downloadFleetFrame(env, vm);
    }

    // ── fleet input (per-VM command queue in the DO) ──────────────
    if (path === "/fleet/poll" && request.method === "GET") {
      return stub.fetch(request); // host long-poll
    }
    if (path.startsWith("/fleet/input/")) {
      return stub.fetch(request);
    }

    // ── usage event ingestion / inspection ───────────────────────
    if (path === "/usage" && request.method === "POST") {
      if (env.ALLOW_USAGE_UPLOADS !== "1") return json({ ok: false, error: "usage uploads disabled" }, 403);
      return ingestUsageEvent(env, request);
    }
    if (path === "/usage/recent" && request.method === "GET") {
      return stub.fetch(request);
    }
    if (path.startsWith("/usage/artifacts/")) {
      const name = decodeURIComponent(path.slice("/usage/artifacts/".length));
      if (request.method === "PUT") {
        if (env.ALLOW_USAGE_UPLOADS !== "1") return json({ ok: false, error: "usage uploads disabled" }, 403);
        return uploadUsageArtifact(env, name, request);
      }
      if (request.method === "GET") return downloadUsageArtifact(env, name);
    }

    // ── control routes with trace logging ────────────────────────
    if (path === "/cmd" && request.method === "POST") return stub.fetch(request);
    if (path.startsWith("/cmd/") && path.endsWith("/result") && request.method === "POST") {
      return stub.fetch(request);
    }

    // ── everything else goes to the Durable Object ────────────────
    return stub.fetch(request);
  },
};

// ── R2 helpers ───────────────────────────────────────────────────────

async function listSnapshots(env: Env): Promise<Response> {
  const list = await env.SNAPSHOTS.list({ prefix: "snapshots/" });
  const items = list.objects.map((o) => ({
    name: o.key.replace("snapshots/", ""),
    size: o.size,
    uploaded: o.uploaded.toISOString(),
  }));
  return json({ ok: true, snapshots: items });
}

interface FleetInputBody {
  action?: string;
  nx?: number;
  ny?: number;
  text?: string;
  key?: string;
}

function allowedFleetInput(body: FleetInputBody | null): boolean {
  if (!body || typeof body.action !== "string") return false;
  if (body.action === "click" || body.action === "move") {
    return typeof body.nx === "number" && Number.isFinite(body.nx) && body.nx >= 0 && body.nx <= 1
      && typeof body.ny === "number" && Number.isFinite(body.ny) && body.ny >= 0 && body.ny <= 1;
  }
  if (body.action === "type") {
    return typeof body.text === "string" && body.text.length <= 2000;
  }
  if (body.action === "key") {
    return typeof body.key === "string" && body.key.length <= 24
      && ["return", "enter", "escape", "esc", "tab", "delete", "backspace", "space",
          "up", "down", "left", "right", "home", "end", "pageup", "pagedown"].includes(body.key);
  }
  return false;
}

function allowedBrowserCommand(body: { action?: string; params?: Record<string, unknown> } | null): boolean {
  if (!body || !body.params || typeof body.params !== "object" || Array.isArray(body.params)) return false;
  const p = body.params;
  if (body.action === "frame") return Object.keys(p).length === 0;
  if (body.action === "unlock") return Object.keys(p).length === 0;
  if (body.action === "click") return Object.keys(p).sort().join(",") === "nx,ny"
    && [p.nx, p.ny].every(v => typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 1);
  if (body.action === "key") return Object.keys(p).join(",") === "key"
    && typeof p.key === "string" && ["return", "enter", "escape", "tab", "delete", "up", "down", "left", "right", "space"].includes(p.key);
  if (body.action === "type") return Object.keys(p).join(",") === "text"
    && typeof p.text === "string" && p.text.length <= 2000;
  return false;
}

function browserConsole(): Response {
  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>virtualOS guest</title><style>
body{font:15px system-ui;background:#10151b;color:#edf2f7;max-width:1100px;margin:24px auto;padding:0 16px}
button,input{font:inherit;padding:9px;border-radius:7px;border:1px solid #526171;background:#1d2935;color:inherit}
button{cursor:pointer}button:disabled{opacity:.5}input{min-width:14em}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
#frame{width:100%;background:#000;border:1px solid #526171;touch-action:manipulation;cursor:crosshair}
#message{min-height:1.5em;color:#a8d6fe}small{color:#aab8c5}
</style></head><body>
<h1>virtualOS guest</h1><p>Private browser control of the Mac Mini guest. The access key stays in this tab's memory.</p>
<div class="row"><input id="token" type="password" autocomplete="off" placeholder="Browser access key"><button id="connect">Connect</button><button id="disconnect" disabled>Disconnect</button></div>
<div class="row"><button id="refresh" disabled>Refresh frame</button><button id="unlock" disabled>Wake and unlock</button><button id="auto" disabled>Start live view</button><span id="state">Disconnected</span></div>
<img id="frame" alt="Guest screen. Click to control the guest."><p id="message" role="status"></p>
<div class="row"><input id="text" autocomplete="off" placeholder="Text to type in guest"><button id="type" disabled>Type</button><button id="enter" disabled>Return</button><button id="escape" disabled>Escape</button></div>
<small>Frames are returned with commands and discarded after viewing. This page does not save screenshots, keystrokes, or the access key.</small>
<script>
(() => {
  let token = '', timer = null, busy = false;
  const $ = id => document.getElementById(id);
  const message = value => { $('message').textContent = value; };
  const enabled = on => { for (const id of ['refresh','unlock','auto','type','enter','escape','disconnect']) $(id).disabled = !on; };
  const request = async (path, options = {}) => {
    const response = await fetch(path, {...options, headers: {'Authorization':'Bearer '+token,'Content-Type':'application/json'},cache:'no-store'});
    if (!response.ok) throw Error('Request failed ('+response.status+')');
    return response.json();
  };
  const command = async (action, params = {}) => {
    const queued = await request('/cmd',{method:'POST',body:JSON.stringify({action,params})});
    if (!queued.id) throw Error('Command was not queued');
    for (let i=0;i<90;i++) {
      await new Promise(resolve => setTimeout(resolve,500));
      const result = await request('/cmd/'+encodeURIComponent(queued.id));
      if (result.status === 'completed') return result.result;
    }
    throw Error('Guest response timed out');
  };
  const frame = async () => {
    if (!token || busy) return;
    busy=true;
    try {
      const result=await command('frame');
      if (!result?.ok || typeof result.data_url !== 'string' || !result.data_url.startsWith('data:image/jpeg;base64,')) throw Error('Guest frame unavailable');
      $('frame').src=result.data_url;
      $('state').textContent='Connected · '+new Date().toLocaleTimeString();
      message('');
    } catch(error) { message(error.message); }
    finally { busy=false; }
  };
  $('connect').onclick=async()=>{
    token=$('token').value.trim(); $('token').value='';
    if (!token) return;
    try { const status=await request('/status'); if (!status.ok) throw Error('Status unavailable'); enabled(true); $('state').textContent='Connected'; await frame(); }
    catch(error) { token=''; enabled(false); message(error.message); }
  };
  $('disconnect').onclick=()=>{token=''; clearInterval(timer); timer=null; $('frame').removeAttribute('src'); $('state').textContent='Disconnected'; $('auto').textContent='Start live view'; enabled(false);};
  $('refresh').onclick=frame;
  $('unlock').onclick=async()=>{try{message('Waking and unlocking the guest…');const result=await command('unlock');if(!result?.ok)throw Error(result?.error||'Guest unlock failed');await frame();}catch(error){message(error.message);}};
  $('auto').onclick=()=>{if(timer){clearInterval(timer);timer=null;$('auto').textContent='Start live view';}else{timer=setInterval(frame,1200);$('auto').textContent='Stop live view';frame();}};
  $('frame').onclick=async event=>{if(!token || !$('frame').src)return;const r=$('frame').getBoundingClientRect();try{await command('click',{nx:(event.clientX-r.left)/r.width,ny:(event.clientY-r.top)/r.height});await frame();}catch(error){message(error.message);}};
  $('type').onclick=async()=>{try{await command('type',{text:$('text').value});$('text').value='';await frame();}catch(error){message(error.message);}};
  $('enter').onclick=async()=>{try{await command('key',{key:'return'});await frame();}catch(error){message(error.message);}};
  $('escape').onclick=async()=>{try{await command('key',{key:'escape'});await frame();}catch(error){message(error.message);}};
})();
</script></body></html>`;
  return new Response(html, { headers: {
    "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
  } });
}

async function uploadSnapshot(env: Env, name: string, req: Request): Promise<Response> {
  const key = `snapshots/${name}`;
  await env.SNAPSHOTS.put(key, req.body, {
    httpMetadata: { contentType: req.headers.get("Content-Type") ?? "application/octet-stream" },
  });
  return json({ ok: true, key });
}

async function downloadSnapshot(env: Env, name: string): Promise<Response> {
  const obj = await env.SNAPSHOTS.get(`snapshots/${name}`);
  if (!obj) return json({ ok: false, error: "not found" }, 404);
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("Content-Length", String(obj.size));
  return new Response(obj.body, { headers });
}

async function deleteSnapshot(env: Env, name: string): Promise<Response> {
  await env.SNAPSHOTS.delete(`snapshots/${name}`);
  return json({ ok: true, deleted: name });
}

async function uploadScreenshot(env: Env, req: Request): Promise<Response> {
  await env.SNAPSHOTS.put("latest-screenshot.jpg", req.body, {
    httpMetadata: { contentType: "image/jpeg" },
  });
  return json({ ok: true, key: "latest-screenshot.jpg" });
}

async function downloadScreenshot(env: Env): Promise<Response> {
  const obj = await env.SNAPSHOTS.get("latest-screenshot.jpg");
  if (!obj) return json({ ok: false, error: "no screenshot yet" }, 404);
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("Content-Length", String(obj.size));
  headers.set("Cache-Control", "no-cache");
  return new Response(obj.body, { headers });
}

async function uploadFleetFrame(env: Env, vm: string, req: Request): Promise<Response> {
  const key = `fleet/frames/${vm}.jpg`;
  await env.SNAPSHOTS.put(key, req.body, {
    httpMetadata: { contentType: req.headers.get("Content-Type") ?? "image/jpeg" },
  });
  return json({ ok: true, vm, key });
}

async function downloadFleetFrame(env: Env, vm: string): Promise<Response> {
  const obj = await env.SNAPSHOTS.get(`fleet/frames/${vm}.jpg`);
  if (!obj) return json({ ok: false, error: "no frame for vm" }, 404);
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("Content-Length", String(obj.size));
  headers.set("Cache-Control", "no-cache");
  return new Response(obj.body, { headers });
}

async function ingestUsageEvent(env: Env, request: Request): Promise<Response> {
  const body = await safeJson<Record<string, unknown>>(request.clone());
  if (!body) {
    return json({ ok: false, error: "invalid json" }, 400);
  }

  const event = await recordUsageEvent(env, body);
  return json({ ok: true, event });
}

async function uploadUsageArtifact(env: Env, name: string, request: Request): Promise<Response> {
  const key = `usage/artifacts/${name}`;
  await env.SNAPSHOTS.put(key, request.body, {
    httpMetadata: { contentType: request.headers.get("Content-Type") ?? "application/octet-stream" },
  });
  return json({ ok: true, key });
}

async function downloadUsageArtifact(env: Env, name: string): Promise<Response> {
  const obj = await env.SNAPSHOTS.get(`usage/artifacts/${name}`);
  if (!obj) return json({ ok: false, error: "not found" }, 404);
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("Content-Length", String(obj.size));
  headers.set("Cache-Control", "no-cache");
  return new Response(obj.body, { headers });
}

async function recordUsageEvent(env: Env, raw: Record<string, unknown>): Promise<UsageEvent> {
  const event = normalizeUsageEvent(raw);

  try {
    await env.SNAPSHOTS.put(usageEventKey(event), JSON.stringify(event), {
      httpMetadata: { contentType: "application/json" },
    });
  } catch (error) {
    console.error("usage-event persistence failed", error);
  }

  try {
    const id = env.CONTROL.idFromName("singleton");
    const stub = env.CONTROL.get(id);
    await stub.fetch(
      new Request("https://relay.internal/usage", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(event),
      }),
    );
  } catch (error) {
    console.error("usage-event cache update failed", error);
  }

  return event;
}

function normalizeUsageEvent(raw: Record<string, unknown>): UsageEvent {
  const ts = typeof raw.ts === "string" && raw.ts.length > 0 ? raw.ts : new Date().toISOString();
  return {
    id: typeof raw.id === "string" && raw.id.length > 0 ? raw.id : crypto.randomUUID(),
    ts,
    source: typeof raw.source === "string" && raw.source.length > 0 ? raw.source : "unknown",
    kind: typeof raw.kind === "string" && raw.kind.length > 0 ? raw.kind : "event",
    action: typeof raw.action === "string" ? raw.action : undefined,
    commandId: typeof raw.commandId === "string" ? raw.commandId : undefined,
    metadata: redactSensitive(raw.metadata),
  };
}

function redactSensitive(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((entry) => redactSensitive(entry));
  }

  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, entry]) => [
        key,
        SENSITIVE_FIELD_PATTERN.test(key) ? REDACTED : redactSensitive(entry),
      ]),
    );
  }

  return value;
}

function usageEventKey(event: UsageEvent): string {
  const day = event.ts.slice(0, 10);
  const stamp = event.ts.replace(/[:.]/g, "-");
  return `usage/${day}/${stamp}-${event.id}.json`;
}

async function safeJson<T>(request: Request | Response): Promise<T | null> {
  try {
    return (await request.json()) as T;
  } catch {
    return null;
  }
}

// ── Durable Object: command queue + status ───────────────────────────

interface Command {
  id: string;
  action: string;
  params: Record<string, unknown>;
  createdAt: number;
  result?: unknown;
  completedAt?: number;
}

interface MachineStatus {
  state: string;
  vmName?: string;
  cpuCount?: number;
  memoryGB?: number;
  host?: {
    cpuLoad?: number;
    memFreeMB?: number;
    diskFreeGB?: number;
  };
  streamServer?: boolean;
  vmIP?: string;
  windowCount?: number;
  diskAllocatedGB?: number;
  diskLogicalGB?: number;
  monitorStatus?: string;
  recentFailures?: number;
  ts: number;
}

interface HeartbeatEntry {
  ts: number;
  state: string;
}

const STALE_THRESHOLD_MS = 90_000; // 3 missed heartbeats (30s interval)
const HEARTBEAT_HISTORY_MAX = 60; // ~30 min of history
const USAGE_EVENT_HISTORY_MAX = 200;
// A queued input that is polled by the host but never completed (the host died,
// the poller's control thread stalled) must not hang "pending" forever. After
// this long, report it as expired so a client is not misled into waiting.
const INPUT_TTL_MS = 60_000;

interface FleetCommand {
  id: string;
  vm: string;
  action: string;
  params: Record<string, unknown>;
  createdAt: number;
  result?: unknown;
  completedAt?: number;
}

export class ControlRelay implements DurableObject {
  private queue: Command[] = [];
  private results: Map<string, Command> = new Map();
  private status: MachineStatus = { state: "unknown", ts: 0 };
  private fleetStatus: Record<string, unknown> = {};
  private heartbeatHistory: HeartbeatEntry[] = [];
  private usageEvents: UsageEvent[] = [];
  private firstHeartbeatTs = 0;
  private heartbeatCount = 0;
  private lastStateChange = 0;
  private previousState = "unknown";
  private waitingPollers: Array<{ resolve: (r: Response) => void; timer: number }> = [];
  private fleetQueue: FleetCommand[] = [];
  private fleetResults: Map<string, FleetCommand> = new Map();

  // Input TTL is overridable via the INPUT_TTL_MS var so local integration
  // tests can exercise expiry without waiting a full minute.
  private inputTtl(): number {
    const override = Number(this.env?.INPUT_TTL_MS);
    return Number.isFinite(override) && override > 0 ? override : INPUT_TTL_MS;
  }

  constructor(private state: DurableObjectState, private env: Env = {} as Env) {
    // Restore the fleet input queue so commands queued while the host poller
    // is reconnecting are not lost across DO eviction or redeploys.
    this.state.blockConcurrencyWhile(async () => {
      const stored = await this.state.storage.get<FleetCommand[]>("fleetQueue");
      if (stored && stored.length > 0) this.fleetQueue = stored;
    });
  }

  private async persistFleetQueue() {
    await this.state.storage.put("fleetQueue", this.fleetQueue);
  }

  private async persistFleetResult(cmd: FleetCommand) {
    await this.state.storage.put(`fleetResult:${cmd.id}`, cmd);
    // keep the persisted result set bounded
    const list = await this.state.storage.list<FleetCommand>({ prefix: "fleetResult:", limit: 200 });
    const keys = [...list.keys()];
    if (keys.length > 100) {
      for (const key of keys.slice(0, keys.length - 100)) {
        await this.state.storage.delete(key);
      }
    }
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // POST /cmd — client queues a command
    if (path === "/cmd" && method === "POST") {
      const body = (await request.json()) as { action: string; params?: Record<string, unknown> };
      const cmd: Command = {
        id: crypto.randomUUID(),
        action: body.action,
        params: body.params ?? {},
        createdAt: Date.now(),
      };
      this.queue.push(cmd);
      this.results.set(cmd.id, cmd);
      this.wakePollers();
      return json({ ok: true, id: cmd.id });
    }

    // POST /usage — top-level worker forwards normalized usage events here
    if (path === "/usage" && method === "POST") {
      const body = normalizeUsageEvent((await request.json()) as Record<string, unknown>);
      this.usageEvents.push(body);
      if (this.usageEvents.length > USAGE_EVENT_HISTORY_MAX) {
        this.usageEvents = this.usageEvents.slice(-USAGE_EVENT_HISTORY_MAX);
      }
      return json({ ok: true });
    }

    // GET /usage/recent — lightweight inspection for training traces
    if (path === "/usage/recent" && method === "GET") {
      const requested = Number(url.searchParams.get("limit") ?? "50");
      const limit = Number.isFinite(requested) ? Math.min(Math.max(Math.trunc(requested), 1), USAGE_EVENT_HISTORY_MAX) : 50;
      return json({ ok: true, events: this.usageEvents.slice(-limit).reverse() });
    }

    // GET /cmd/poll — mac long-polls for next command
    if (path === "/cmd/poll" && method === "GET") {
      const next = this.queue.shift();
      if (next) return json({ ok: true, command: next });

      // long-poll: wait up to 25s for a command
      const timeoutMs = 25_000;
      return new Promise<Response>((resolve) => {
        const timer = setTimeout(() => {
          this.removePoller(resolve);
          resolve(json({ ok: true, command: null }));
        }, timeoutMs) as unknown as number;
        this.waitingPollers.push({ resolve, timer });
      });
    }

    // POST /cmd/:id/result — mac posts result
    if (path.startsWith("/cmd/") && path.endsWith("/result") && method === "POST") {
      const id = path.split("/")[2];
      const body = await request.json();
      const cmd = this.results.get(id) ?? { id, action: "unknown", params: {}, createdAt: 0 };
      cmd.result = body;
      cmd.completedAt = Date.now();
      this.results.set(id, cmd);
      // keep last 100 results
      if (this.results.size > 100) {
        const oldest = this.results.keys().next().value;
        if (oldest) this.results.delete(oldest);
      }
      return json({ ok: true });
    }

    // GET /cmd/:id — client checks result
    if (path.startsWith("/cmd/") && method === "GET") {
      const id = path.split("/")[2];
      const cmd = this.results.get(id);
      if (!cmd) return json({ ok: true, status: "pending" });
      if (cmd.completedAt) {
        if (cmd.action === "frame") this.results.delete(id);
        return json({ ok: true, status: "completed", result: cmd.result });
      }
      if (Date.now() - cmd.createdAt > this.inputTtl()) {
        cmd.completedAt = Date.now();
        cmd.result = { ok: false, status: "expired", reason: "host did not complete command in time" };
        this.results.set(id, cmd);
        return json({ ok: true, status: "completed", result: cmd.result });
      }
      return json({ ok: true, status: "pending" });
    }

    // POST /status — mac pushes heartbeat
    if (path === "/status" && method === "POST") {
      const body = (await request.json()) as MachineStatus;
      const now = Date.now();
      const newState = body.state ?? "unknown";

      // Track state transitions
      if (newState !== this.previousState) {
        this.lastStateChange = now;
        this.previousState = newState;
      }

      this.status = { ...body, ts: now };

      // Track heartbeat history
      if (this.firstHeartbeatTs === 0) this.firstHeartbeatTs = now;
      this.heartbeatCount++;
      this.heartbeatHistory.push({ ts: now, state: newState });
      if (this.heartbeatHistory.length > HEARTBEAT_HISTORY_MAX) {
        this.heartbeatHistory = this.heartbeatHistory.slice(-HEARTBEAT_HISTORY_MAX);
      }

      return json({ ok: true });
    }

    // GET /status — client reads status with liveness info
    if (path === "/status" && method === "GET") {
      const now = Date.now();
      const lastTs = this.status.ts || 0;
      const ageSec = lastTs > 0 ? Math.round((now - lastTs) / 1000) : -1;
      const isStale = lastTs === 0 || (now - lastTs) > STALE_THRESHOLD_MS;

      // Calculate uptime percentage from heartbeat history
      let uptimePct = 0;
      if (this.heartbeatHistory.length > 1) {
        const runningCount = this.heartbeatHistory.filter((h) => h.state === "running").length;
        uptimePct = Math.round((runningCount / this.heartbeatHistory.length) * 100);
      }

      // Compute avg heartbeat interval
      let avgIntervalSec = 0;
      if (this.heartbeatHistory.length > 1) {
        const first = this.heartbeatHistory[0].ts;
        const last = this.heartbeatHistory[this.heartbeatHistory.length - 1].ts;
        avgIntervalSec = Math.round((last - first) / (this.heartbeatHistory.length - 1) / 1000);
      }

      return json({
        ok: true,
        machine: this.status,
        liveness: {
          lastHeartbeatAgeSec: ageSec,
          isStale,
          staleThresholdSec: STALE_THRESHOLD_MS / 1000,
          totalHeartbeats: this.heartbeatCount,
          uptimePct,
          avgIntervalSec,
          lastStateChange: this.lastStateChange,
          recentHistory: this.heartbeatHistory.slice(-10),
        },
      });
    }

    // POST /fleet/status — mac pushes multi-VM fleet heartbeat
    if (path === "/fleet/status" && method === "POST") {
      const body = (await request.json()) as Record<string, unknown>;
      this.fleetStatus = { ...body, ts: Date.now() };
      return json({ ok: true });
    }

    // GET /fleet/status — client reads fleet status
    if (path === "/fleet/status" && method === "GET") {
      const now = Date.now();
      const lastTs = (this.fleetStatus.ts as number) || 0;
      const ageSec = lastTs > 0 ? Math.round((now - lastTs) / 1000) : -1;
      return json({
        ok: true,
        fleet: this.fleetStatus,
        liveness: {
          lastHeartbeatAgeSec: ageSec,
          isStale: lastTs === 0 || (now - lastTs) > STALE_THRESHOLD_MS,
        },
      });
    }

    // GET /fleet/poll — mac polls for next fleet input command.
    // Returns immediately so it can never block a concurrent /fleet/input POST
    // (Durable Object input-gate serialization otherwise deadlocks the queue).
    if (path === "/fleet/poll" && method === "GET") {
      const next = this.fleetQueue.shift();
      if (next) {
        await this.persistFleetQueue();
        return json({ ok: true, command: next });
      }
      return json({ ok: true, command: null });
    }

    // POST /fleet/input/:vm/result/:id — mac posts the applied result
    if (path.startsWith("/fleet/input/") && path.includes("/result/") && method === "POST") {
      const id = path.split("/")[5];
      const body = await request.json();
      const cmd = this.fleetResults.get(id)
        ?? { id, vm: "unknown", action: "unknown", params: {}, createdAt: 0 };
      cmd.result = body;
      cmd.completedAt = Date.now();
      this.fleetResults.set(id, cmd);
      await this.persistFleetResult(cmd);
      return json({ ok: true });
    }

    if (path.startsWith("/fleet/input/") && method === "POST") {
      const vm = decodeURIComponent(path.slice("/fleet/input/".length));
      if (!/^[a-z0-9._-]{1,64}$/i.test(vm)) {
        return json({ ok: false, error: "invalid vm name" }, 400);
      }
      const body = (await request.json()) as FleetInputBody;
      const cmd: FleetCommand = {
        id: crypto.randomUUID(),
        vm,
        action: body.action ?? "unknown",
        params: {
          nx: body.nx, ny: body.ny, text: body.text, key: body.key,
        },
        createdAt: Date.now(),
      };
      this.fleetQueue.push(cmd);
      this.fleetResults.set(cmd.id, cmd);
      await this.persistFleetQueue();
      await this.persistFleetResult(cmd);
      return json({ ok: true, id: cmd.id, vm, action: cmd.action });
    }

    // GET /fleet/input/:vm/:id — client reads input result
    if (path.startsWith("/fleet/input/") && method === "GET") {
      const parts = path.split("/");
      const id = parts[4];
      let cmd = this.fleetResults.get(id);
      if (!cmd) {
        cmd = (await this.state.storage.get<FleetCommand>(`fleetResult:${id}`)) ?? undefined;
      }
      if (!cmd) return json({ ok: true, status: "pending" });
      if (cmd.completedAt) {
        return json({ ok: true, status: "completed", result: cmd.result });
      }
      // Never completed: expire it after the TTL so clients stop waiting.
      if (Date.now() - cmd.createdAt > this.inputTtl()) {
        cmd.completedAt = Date.now();
        cmd.result = { ok: false, status: "expired", reason: "host did not complete input in time" };
        this.fleetResults.set(id, cmd);
        await this.persistFleetResult(cmd);
        return json({ ok: true, status: "completed", result: cmd.result });
      }
      return json({ ok: true, status: "pending" });
    }

    // GET /status/history — full heartbeat history
    if (path === "/status/history" && method === "GET") {
      return json({
        ok: true,
        firstHeartbeat: this.firstHeartbeatTs,
        totalHeartbeats: this.heartbeatCount,
        history: this.heartbeatHistory,
      });
    }

    // GET /status/alerts — check for problems
    if (path === "/status/alerts" && method === "GET") {
      const now = Date.now();
      const alerts: string[] = [];

      // Stale heartbeat
      if (this.status.ts === 0) {
        alerts.push("no heartbeat received yet");
      } else if (now - this.status.ts > STALE_THRESHOLD_MS) {
        const mins = Math.round((now - this.status.ts) / 60000);
        alerts.push(`heartbeat stale (last seen ${mins}m ago)`);
      }

      // VM not running
      if (this.status.state && this.status.state !== "running" && this.status.state !== "unknown") {
        alerts.push(`vm state: ${this.status.state}`);
      }

      // Low disk
      if (this.status.host?.diskFreeGB !== undefined && this.status.host.diskFreeGB < 15) {
        alerts.push(`low disk: ${this.status.host.diskFreeGB} GB free`);
      }

      // Stream server down
      if (this.status.streamServer === false) {
        alerts.push("stream server not running");
      }

      // Recent failures
      if (this.status.recentFailures && this.status.recentFailures > 0) {
        alerts.push(`${this.status.recentFailures} recent VM failures`);
      }

      return json({
        ok: alerts.length === 0,
        alerts,
        status: this.status.state ?? "unknown",
        lastHeartbeatAgeSec: this.status.ts ? Math.round((now - this.status.ts) / 1000) : -1,
      });
    }

    return json({ ok: false, error: "not found" }, 404);
  }

  private wakePollers() {
    while (this.waitingPollers.length > 0 && this.queue.length > 0) {
      const poller = this.waitingPollers.shift()!;
      clearTimeout(poller.timer);
      const cmd = this.queue.shift()!;
      poller.resolve(json({ ok: true, command: cmd }));
    }
  }

  private removePoller(resolve: (r: Response) => void) {
    const idx = this.waitingPollers.findIndex((p) => p.resolve === resolve);
    if (idx !== -1) this.waitingPollers.splice(idx, 1);
  }
}

// ── util ─────────────────────────────────────────────────────────────

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}
