import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const ts = require('typescript');

function client(fetch) {
  const source = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
    .replaceAll('import.meta.env', '({})');
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  new Function('exports', 'fetch', 'window', code)(exports, fetch, { location: { host: 'localhost' } });
  return exports;
}

test('connection failures are distinguished from HTTP errors and recover on a later read', async () => {
  let online = false;
  const api = client(async () => {
    if (!online) throw new TypeError('Failed to fetch');
    return { ok: true, json: async () => ({ success: true }) };
  });
  await assert.rejects(api.apiFetch('/api/account-usage'), { name: 'ApiConnectionError' });
  online = true;
  assert.deepEqual(await api.apiFetch('/api/account-usage'), { success: true });
  const rejected = client(async () => ({ ok: false, json: async () => ({ detail: 'Login required' }) }));
  await assert.rejects(rejected.apiFetch('/api/account-usage'), { name: 'Error', message: 'Login required' });
});

test('abort remains cancellation and writes are never automatically retried', async () => {
  const aborted = client(async () => { throw new DOMException('cancelled', 'AbortError'); });
  await assert.rejects(aborted.apiFetch('/api/account-usage'), { name: 'AbortError' });
  let attempts = 0;
  const offline = client(async () => { attempts += 1; throw new TypeError('network error'); });
  await assert.rejects(offline.apiFetch('/api/account-usage/capture', { method: 'POST' }));
  assert.equal(attempts, 1);
});

test('a connection closed after response headers is classified without replaying the request', async () => {
  let response;
  let attempts = 0;
  const server = createServer((_request, res) => {
    response = res;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.write('{"success":');
  });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  const api = client(async (url, options) => {
    attempts += 1;
    const result = await fetch(url, options);
    response.destroy();
    return result;
  });
  try {
    await assert.rejects(api.apiFetch(`http://127.0.0.1:${server.address().port}/capture`, {
      method: 'POST', body: '{}',
    }), { name: 'ApiConnectionError' });
    assert.equal(attempts, 1);
  } finally {
    server.closeAllConnections();
    await new Promise((done) => server.close(done));
  }
});

test('body cancellation and invalid JSON are not mislabeled as a lost connection', async () => {
  for (const error of [new DOMException('cancelled', 'AbortError'), new SyntaxError('Invalid JSON')]) {
    const api = client(async () => ({ ok: true, json: async () => { throw error; } }));
    await assert.rejects(api.apiFetch('/api/account-usage'), (actual) => actual === error);
  }
});
