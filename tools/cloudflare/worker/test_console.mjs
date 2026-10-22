// Run: node --experimental-transform-types --test test_console.mjs
import assert from 'node:assert/strict';
import { test } from 'node:test';
import vm from 'node:vm';
import worker from './src/index.ts';

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

async function harness({ stale = false, heldFrame = null, noReady = false } = {}) {
  const env = { CONTROL: { idFromName() {}, get() {} } };
  const page = await worker.fetch(new Request('https://relay.test/console'), env);
  assert.match(page.headers.get('Content-Security-Policy'), /img-src data: blob:/);
  const html = await page.text();
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { value: '', textContent: '', disabled: true, children: [],
      replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); },
      removeAttribute(key) { delete this[key]; },
      getBoundingClientRect() { return { left: 0, top: 0, width: 100, height: 100 }; } });
    return elements.get(id);
  };
  const calls = [], revoked = [], frameRequested = deferred();
  let image = 0;
  class BrowserURL extends URL {}
  BrowserURL.createObjectURL = () => 'blob:fixture-' + (++image);
  BrowserURL.revokeObjectURL = url => revoked.push(url);
  const frame = () => new Response(new Uint8Array([255, 216, 255, 217]), { headers: {
    'Content-Type': 'image/jpeg', 'X-VOS-Frame-Captured-At': String(Date.now() - (stale ? 60000 : 0)),
  } });
  const response = body => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
  const fetch = async (path, options = {}) => {
    calls.push({ path, options });
    if (path === '/fleet/status') return response({ ok: true, liveness: { isStale: false }, fleet: { instances: [
      { name: 'vm-a', ready: !noReady, state: 'running' },
      { name: 'vm-b', ready: !noReady, state: 'running' },
    ] } });
    if (path.startsWith('/fleet/frame/')) {
      frameRequested.resolve();
      return heldFrame ? heldFrame.promise : frame();
    }
    if (path === '/fleet/input/vm-b' && options.method === 'POST') return response({ ok: true, id: 'fixture-id' });
    if (path === '/fleet/input/vm-b/fixture-id') return response({
      ok: true, status: 'completed', completedAt: Date.now() - 1, result: { ok: true, status: 200 },
    });
    throw Error('Unexpected route ' + path);
  };
  vm.runInNewContext(script, { document: { getElementById: element, createElement: () => ({}) },
    location: { search: '?vm=vm-b' }, URLSearchParams, URL: BrowserURL, fetch,
    setTimeout: fn => { queueMicrotask(fn); return 1; }, clearInterval() {}, setInterval() { return 1; } });
  element('token').value = 'private-fixture';
  return { element, calls, revoked, frame, frameRequested };
}

test('console chooses a fleet VM and sends normalized input through its fleet queue', async () => {
  const h = await harness();
  await h.element('connect').onclick();
  assert.equal(h.element('machine').value, 'vm-b');
  assert.equal(h.element('token').value, '');
  assert.equal(h.element('refresh').disabled, false);
  assert.match(h.element('frame').src, /^blob:/);
  await h.element('frame').onclick({ clientX: 25, clientY: 75 });
  const sent = h.calls.find(call => call.options.method === 'POST');
  assert.equal(sent.path, '/fleet/input/vm-b');
  assert.deepEqual(JSON.parse(sent.options.body), { action: 'click', nx: .25, ny: .75 });
  assert.equal(sent.options.headers.Authorization, 'Bearer private-fixture');
  assert.equal(h.calls.some(call => call.path.startsWith('/cmd')), false);
});

test('disconnect clears local data and an in-flight frame cannot restore the image', async () => {
  const held = deferred(), h = await harness({ heldFrame: held });
  const connecting = h.element('connect').onclick();
  await h.frameRequested.promise;
  h.element('text').value = 'private pending text';
  h.element('disconnect').onclick();
  held.resolve(h.frame());
  await connecting;
  assert.equal(h.element('frame').src, undefined);
  assert.equal(h.element('text').value, '');
  assert.equal(h.element('machine').children.length, 0);
  assert.equal(h.element('refresh').disabled, true);
  assert.equal(h.element('state').textContent, 'Disconnected');
});

test('stale frames refuse control before any command is queued', async () => {
  const h = await harness({ stale: true });
  await h.element('connect').onclick();
  await h.element('enter').onclick();
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
  assert.match(h.element('message').textContent, /Frame is stale/);
});

test('typed text targets the selected VM and is cleared after acknowledgement', async () => {
  const h = await harness();
  await h.element('connect').onclick();
  h.element('text').value = 'fixture text';
  await h.element('type').onclick();
  const sent = h.calls.find(call => call.options.method === 'POST');
  assert.deepEqual(JSON.parse(sent.options.body), { action: 'type', text: 'fixture text' });
  assert.equal(h.element('text').value, '');
  h.element('disconnect').onclick();
  assert.ok(h.revoked.length > 0);
});

test('overlong text is refused before it can be silently truncated by the guest', async () => {
  const h = await harness();
  await h.element('connect').onclick();
  h.element('text').value = 'x'.repeat(513);
  await h.element('type').onclick();
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
  assert.match(h.element('message').textContent, /512/);
});

test('no ready machine leaves all guest controls disabled', async () => {
  const h = await harness({ noReady: true });
  await h.element('connect').onclick();
  assert.equal(h.element('enter').disabled, true);
  assert.equal(h.element('frame').src, undefined);
  assert.match(h.element('message').textContent, /No ready machines/);
});
