import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const source = readFileSync(new URL('../src/lib/nodes.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
} }).outputText;
const nodes = {};
new Function('exports', code)(nodes);

const center = { role: 'center', node_id: 'mini-c0de', nodes: ['mini-c0de', 'air-ed9e'], center_url: '' };
const rows = [{ id: 1, origin_node: 'air-ed9e' }, { id: 2, origin_node: 'mini-c0de' }, { id: 3, origin_node: '' },
  { id: 4 }];

test('a standalone node shows no origins and offers no choice', () => {
  const standalone = { role: 'standalone', node_id: '', nodes: [], center_url: '' };
  assert.equal(nodes.showsOrigins(standalone), false);
  assert.equal(nodes.showsOrigins(undefined), false);
  assert.deepEqual(nodes.nodeChoices(standalone), []);
});

test('a syncing node offers its nodes and the rows from before the cut-over', () => {
  assert.equal(nodes.showsOrigins(center), true);
  assert.deepEqual(nodes.nodeChoices(center), ['mini-c0de', 'air-ed9e', '']);
});

test('the filter keeps one node, the rows from before the cut-over, or everything', () => {
  assert.deepEqual(nodes.byNode(rows, null).map((row) => row.id), [1, 2, 3, 4]);
  assert.deepEqual(nodes.byNode(rows, 'air-ed9e').map((row) => row.id), [1]);
  assert.deepEqual(nodes.byNode(rows, '').map((row) => row.id), [3, 4]);
});

test('a list request names the chosen node, and an empty one too', () => {
  assert.deepEqual(nodes.originParam(null), {});
  assert.deepEqual(nodes.originParam('air-ed9e'), { origin: 'air-ed9e' });
  assert.deepEqual(nodes.originParam(''), { origin: '' });
});
