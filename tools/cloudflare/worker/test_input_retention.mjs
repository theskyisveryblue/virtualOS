// Run: node --experimental-transform-types --test test_input_retention.mjs
import assert from 'node:assert/strict';
import { test } from 'node:test';
import worker, { ControlRelay } from './src/index.ts';

function relay() {
  const records = new Map();
  let ready;
  const state = {
    storage: {
      async get(key) { return structuredClone(records.get(key)); },
      async put(key, value) { records.set(key, structuredClone(value)); },
      async delete(key) { records.delete(key); },
      async list({ prefix }) {
        return new Map([...records].filter(([key]) => key.startsWith(prefix)));
      },
    },
    blockConcurrencyWhile(callback) { ready = callback(); },
  };
  return { control: new ControlRelay(state, {}), records, ready: () => ready };
}

function request(path, payload) {
  return new Request(`https://relay.test${path}`, payload === undefined ? {} : {
    method: 'POST', body: JSON.stringify(payload),
  });
}

test('fleet delivers text once and removes its payload from completed records', async () => {
  const { control, records, ready } = relay();
  await ready();
  const queued = await (await control.fetch(request('/fleet/input/vm-a', {
    action: 'type', text: 'private-fixture',
  }))).json();
  const polled = await (await control.fetch(request('/fleet/poll'))).json();
  assert.equal(polled.command.params.text, 'private-fixture');
  await control.fetch(request(`/fleet/input/vm-a/result/${queued.id}`, { ok: true, status: 200 }));
  assert.deepEqual(records.get(`fleetResult:${queued.id}`).params, {});
  assert.deepEqual(control.fleetResults.get(queued.id).params, {});
  assert.deepEqual(records.get('fleetQueue'), []);
  const completed = await control.fetch(request(`/fleet/input/vm-a/${queued.id}`));
  assert.equal(completed.headers.get('Cache-Control'), 'private, no-store');
  const result = await completed.json();
  assert.equal(result.status, 'completed');
  assert.equal(result.result.ok, true);
  assert.equal(typeof result.completedAt, 'number');
});

test('single guest completion removes typed payload from memory', async () => {
  const { control, ready } = relay();
  await ready();
  const queued = await (await control.fetch(request('/cmd', {
    action: 'type', params: { text: 'private-fixture' },
  }))).json();
  const polled = await (await control.fetch(request('/cmd/poll'))).json();
  assert.equal(polled.command.params.text, 'private-fixture');
  await control.fetch(request(`/cmd/${queued.id}/result`, { ok: true }));
  assert.deepEqual(control.results.get(queued.id).params, {});
});

test('reading a historical completed fleet result scrubs its stored payload', async () => {
  const { control, records, ready } = relay();
  await ready();
  records.set('fleetResult:old', { id: 'old', vm: 'vm-a', action: 'type',
    params: { text: 'historical-fixture' }, createdAt: 1, completedAt: 2, result: { ok: true } });
  const result = await (await control.fetch(request('/fleet/input/vm-a/old'))).json();
  assert.equal(result.status, 'completed');
  assert.deepEqual(records.get('fleetResult:old').params, {});
});

test('fleet frames expose capture age and prohibit browser storage', async () => {
  let saved;
  const env = {
    AUTH_TOKEN: 'host', WEB_TOKEN: 'browser',
    CONTROL: { idFromName() { return 'fixture'; }, get() { return {}; } },
    SNAPSHOTS: {
      async put(key, body, options) {
        saved = { key, body: await new Response(body).arrayBuffer(), ...options };
      },
      async get(key) {
        assert.equal(key, saved.key);
        return { body: saved.body, size: saved.body.byteLength,
          customMetadata: saved.customMetadata, httpEtag: '"fixture-etag"',
          uploaded: new Date('2026-01-01T00:00:00Z'),
          writeHttpMetadata(headers) { headers.set('Content-Type', 'image/jpeg'); } };
      },
    },
  };
  const capturedAt = String(Date.now() - 5000);
  await worker.fetch(new Request('https://relay.test/fleet/frame/vm-a', {
    method: 'PUT', body: 'frame', headers: { Authorization: 'Bearer host',
      'Content-Type': 'image/jpeg', 'X-VOS-Frame-Captured-At': capturedAt },
  }), env);
  const frame = await worker.fetch(new Request('https://relay.test/fleet/frame/vm-a', {
    headers: { Authorization: 'Bearer browser' },
  }), env);
  assert.equal(frame.headers.get('X-VOS-Frame-Captured-At'), capturedAt);
  assert.equal(frame.headers.get('X-VOS-Frame-Uploaded-At'), '2026-01-01T00:00:00.000Z');
  assert.equal(frame.headers.get('Cache-Control'), 'private, no-store');
  assert.equal(frame.headers.get('ETag'), '"fixture-etag"');
  assert.equal(await frame.text(), 'frame');
  await worker.fetch(new Request('https://relay.test/fleet/frame/vm-a', {
    method: 'PUT', body: 'frame', headers: { Authorization: 'Bearer host',
      'X-VOS-Frame-Captured-At': String(Date.now() + 60000) },
  }), env);
  assert.deepEqual(saved.customMetadata, {});
});
