import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const ts = require('typescript');
const source = readFileSync(new URL('../src/components/teams/TeamDisplayName.tsx', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;

function render(team, run) {
  const calls = [];
  const exports = {};
  new Function('require', 'exports', code)((name) => name === '@/api/workflows'
    ? { useWorkflow: (wfId) => { calls.push(wfId); return { data: run }; } }
    : require(name), exports);
  return { html: renderToStaticMarkup(React.createElement(exports.TeamDisplayName, { team })), calls };
}

test('workflow display names prefer the configured run ID and retain the tracing ID', () => {
  const { html, calls } = render({ name: 'workflow-old-id', config: { workflow_run_id: 'actual-id' } },
    { name: 'Observed workflow title' });
  assert.deepEqual(calls, ['actual-id']);
  assert.match(html, /Observed workflow title/);
  assert.match(html, /font-mono[^>]*>actual-id<\/span>/);
  assert.doesNotMatch(html, /old-id/);
});

test('workflow display names fall back to the team name while observation is unavailable', () => {
  for (const run of [undefined, { name: '' }]) {
    const { html, calls } = render({ name: 'workflow-fallback-id' }, run);
    assert.deepEqual(calls, ['fallback-id']);
    assert.match(html, />workflow-fallback-id<\/span>/);
    assert.match(html, /font-mono[^>]*>fallback-id<\/span>/);
  }
});

test('ordinary teams and session fallback teams keep plain names without a run query', () => {
  for (const name of ['Product team', 'workflow-session-a1b2c3d4', 'session-a1b2c3d4']) {
    const { html, calls } = render({ name }, { name: 'Unrelated run' });
    assert.equal(html, name);
    assert.deepEqual(calls, ['']); // The useWorkflow hook disables its query for an empty ID.
  }
});

test('an explicit run ID can identify a workflow whose team has a descriptive name', () => {
  const { html, calls } = render({ name: 'Review team', config: { workflow_run_id: 'review-run' } },
    { name: 'Observed review' });
  assert.deepEqual(calls, ['review-run']);
  assert.match(html, /Observed review/);
  assert.match(html, /font-mono[^>]*>review-run<\/span>/);
});
