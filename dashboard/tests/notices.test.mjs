import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const ts = require('typescript');

function compile(relative, mocks = {}) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8')
    .replaceAll('import.meta.env', '({})');
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  new Function('require', 'exports', code)((name) => mocks[name] ?? require(name), exports);
  return exports;
}

const client = { apiFetch: async () => ({}) };
const notices = compile('../src/api/notices.ts', { './client': client });
const briefings = compile('../src/api/briefings.ts', { './client': client });

test('notice text drops the terminal prefix only at the start', () => {
  assert.equal(notices.noticeText('[AI Team OS] 服务未启动'), '服务未启动');
  assert.equal(notices.noticeText('plain [AI Team OS] '), 'plain [AI Team OS] ');
});

test('notice paths carry the Dashboard language and filters', () => {
  assert.equal(notices.noticeSummaryPath('zh'), '/api/notices/summary?language=zh');
  assert.equal(
    notices.noticeListPath({ status: 'all', group: 'immediate', language: 'en', limit: 20 }),
    '/api/notices?status=all&language=en&limit=20&group=immediate',
  );
  assert.equal(
    notices.noticeListPath({ kind: ['action', 'decision'], language: 'zh' }),
    '/api/notices?status=active&language=zh&limit=50&kind=action%2Cdecision',
  );
});

test('notice keys with folder paths stay routable', () => {
  assert.equal(
    notices.noticeActionPath('unregistered_dir:/Users/a b/项目', 'snooze', 24),
    '/api/notices/unregistered_dir:%2FUsers%2Fa%20b%2F%E9%A1%B9%E7%9B%AE/snooze?hours=24',
  );
  assert.equal(notices.noticeActionPath('api_version_stale:1:2', 'dismiss'),
    '/api/notices/api_version_stale:1:2/dismiss');
});

test('decisions tab hides automatic permission denials unless asked', () => {
  assert.equal(briefings.briefingsPath('pending', undefined, undefined, true),
    '/api/leader-briefings?status=pending&real_only=true');
  assert.equal(briefings.briefingsPath('pending'), '/api/leader-briefings?status=pending');
  // "all" is sent explicitly: the API's default is pending.
  assert.equal(briefings.briefingsPath('all', 'p1', 'release'),
    '/api/leader-briefings?status=all&project_id=p1&tag=release');
});
