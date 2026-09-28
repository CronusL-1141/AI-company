import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const ts = require('typescript');

function client(body) {
  const source = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
    .replaceAll('import.meta.env', '({})');
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  const fetch = async () => ({ ok: true, json: async () => body });
  new Function('exports', 'fetch', 'window', code)(exports, fetch, { location: { host: 'localhost' } });
  return exports;
}

test('a 2xx memo-style refusal is an error, not a success', async () => {
  const finding = '内容安全扫描拒绝写入：命中 不可见字符 U+202E (RIGHT-TO-LEFT OVERRIDE)（第 5 字处）';
  const api = client({
    success: false,
    error: finding,
    safety: { category: 'invisible_unicode', pattern: '不可见字符 U+202E (RIGHT-TO-LEFT OVERRIDE)', field: 'description' },
  });
  await assert.rejects(api.apiFetch('/api/teams/t1/tasks/run', { method: 'POST', body: '{}' }), (error) => {
    assert.equal(error.name, 'ApiRefusedError');
    assert.equal(error.message, finding);
    assert.equal(error.safety.field, 'description');
    return true;
  });
});

test('the reason is taken from the fields a refusal can carry', async () => {
  await assert.rejects(client({ success: false, data: { ok: false, error: 'settings.json 解析失败' } })
    .apiFetch('/api/models/default'), { name: 'ApiRefusedError', message: 'settings.json 解析失败' });
  await assert.rejects(client({ success: false, status: 'refused', reason: 'missing target_session_id' })
    .apiFetch('/api/fleet/dispatch'), { message: 'missing target_session_id' });
  await assert.rejects(client({ success: false, safety: { pattern: 'U+200B' } }).apiFetch('/x'),
    { message: 'API request refused (U+200B)' });
});

test('successful and shapeless bodies still come back as they are', async () => {
  for (const body of [{ success: true, data: { id: 'a' } }, [{ id: 'a' }], { data: [] }, { found: false }, null]) {
    assert.deepEqual(await client(body).apiFetch('/x'), body);
  }
});
