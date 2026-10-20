import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const ts = require('typescript');
const SRC = fileURLToPath(new URL('../src/', import.meta.url));
const PAGE = 20;

const team = (id, projectId) => ({ id, name: id, description: '', project_id: projectId, status: 'active',
  config: {}, max_agents: 20, created_at: '2026-09-10T10:00:00Z' });
const project = (id) => ({ id, name: id, root_path: `/test/${id}`, description: '', created_at: '2026-09-10T10:00:00Z' });
// Rows and envelope follow GET /api/analytics/agent-productivity.
const row = (index) => ({ agent_id: `agent-${index}`, agent_name: `agent-${index}`, activity_count: 100 - index,
  tools_used: 3, last_active: '2026-10-13T10:00:00+00:00' });
const page = (total, offset, limit = PAGE) => ({ success: true, total, total_activities: total * 10, limit, offset,
  data: Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, index) => row(offset + index)) });

// One stateful component: hook slots persist across renders, effects run after a
// render when their dependencies change, and UI primitives keep their props.
function harness({ lang = 'zh', totals = () => 45 } = {}) {
  const slots = [];
  const effectDeps = [];
  let slotIndex = 0;
  let effectIndex = 0;
  let pending = [];
  const requests = [];
  let translations;
  const snapshot = (data) => ({ data, error: null, isLoading: false, isFetching: false });
  const primitive = (name) => {
    const component = ({ children }) => React.createElement('div', { 'data-ui': name }, children);
    component.ui = name;
    return component;
  };
  const ui = new Proxy({}, { get: (_, name) => primitive(String(name)) });
  const mocks = {
    react: { ...React, useMemo: (fn) => fn(), useCallback: (fn) => fn,
      useState: (initial) => {
        const index = slotIndex++;
        if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
        return [slots[index], (next) => { slots[index] = typeof next === 'function' ? next(slots[index]) : next; }];
      },
      useEffect: (effect, deps) => { pending.push([effectIndex++, effect, deps]); } },
    'lucide-react': new Proxy({}, { get: () => () => null }),
    '@/i18n': { useT: () => translations },
    '@/api/projects': { useProjects: () => snapshot({ data: [project('project-one'), project('project-two')] }) },
    '@/api/teams': { useTeams: () => snapshot({ data: [team('team-one', 'project-one'), team('team-two', 'project-two')] }) },
    '@/api/analytics': {
      useToolUsage: () => snapshot([{ tool_name: 'Bash', count: 30 }]),
      useAgentProductivity: (teamId, projectId, offset, limit) => {
        requests.push({ teamId, projectId, offset, limit });
        return snapshot(page(totals({ teamId, projectId }), offset, limit));
      },
      useActivityTimeline: () => snapshot([]),
      useEfficiencyMetrics: () => snapshot(undefined),
    },
  };
  const cache = new Map();
  function load(path) {
    let absolute = path.startsWith('@/') ? resolve(SRC, path.slice(2)) : resolve(SRC, path);
    absolute = ['', '.ts', '.tsx', '/index.ts'].map((suffix) => absolute + suffix).find(existsSync);
    assert.ok(absolute, `Missing source: ${path}`);
    if (cache.has(absolute)) return cache.get(absolute).exports;
    const output = ts.transpileModule(readFileSync(absolute, 'utf8'), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    const module = { exports: {} };
    cache.set(absolute, module);
    new Function('require', 'module', 'exports', output)((name) => {
      if (name in mocks) return mocks[name];
      if (name.startsWith('@/components/ui/')) return ui;
      if (name.startsWith('@/')) return load(name);
      if (name.startsWith('.')) {
        const target = resolve(dirname(absolute), name);
        const alias = '@/' + target.slice(SRC.length);
        return alias in mocks ? mocks[alias] : load(target);
      }
      return require(name);
    }, module, module.exports);
    return module.exports;
  }
  translations = load(`i18n/${lang}.ts`)[lang];
  const { AnalyticsPage } = load('pages/AnalyticsPage.tsx');
  function render() {
    slotIndex = 0;
    effectIndex = 0;
    pending = [];
    const tree = AnalyticsPage();
    for (const [index, effect, deps] of pending) {
      const previous = effectDeps[index];
      if (!previous || deps.some((value, position) => !Object.is(value, previous[position]))) {
        effectDeps[index] = deps;
        effect();
      }
    }
    return tree;
  }
  return { t: translations, requests, render };
}

function find(tree, predicate) {
  const result = [];
  (function visit(node) {
    if (node == null || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(visit); return; }
    if (predicate(node)) result.push(node);
    React.Children.forEach(node.props.children, visit);
  })(tree);
  return result;
}

const selects = (tree) => find(tree, (node) => node.type?.ui === 'Select');
const button = (tree, label) => find(tree, (node) => node.type?.ui === 'Button' && node.props.children === label)[0];
const rangeLabel = (tree) => renderToStaticMarkup(tree).match(/(\d+)–(\d+) \/ (\d+)/)?.slice(1).map(Number);

test('ranking pages use common labels and request bounded server pages in both languages', () => {
  for (const lang of ['zh', 'en']) {
    const h = harness({ lang });
    let tree = h.render();
    assert.deepEqual(h.requests.at(-1), { teamId: undefined, projectId: undefined, offset: 0, limit: PAGE });
    assert.deepEqual(rangeLabel(tree), [1, 20, 45]);
    assert.equal(button(tree, h.t.common.previous).props.disabled, true);
    assert.equal(button(tree, h.t.common.next).props.disabled, false);
    button(tree, h.t.common.next).props.onClick();
    button(h.render(), h.t.common.next).props.onClick();
    tree = h.render();
    assert.equal(h.requests.at(-1).offset, 40);
    assert.deepEqual(rangeLabel(tree), [41, 45, 45]);
    assert.equal(button(tree, h.t.common.next).props.disabled, true);
    button(tree, h.t.common.previous).props.onClick();
    h.render();
    assert.equal(h.requests.at(-1).offset, 20);
  }
});

test('changing the project or team filter returns the ranking to its first page', () => {
  const h = harness();
  let tree = h.render();
  button(tree, h.t.common.next).props.onClick();
  tree = h.render();
  assert.equal(h.requests.at(-1).offset, 20);
  selects(tree)[0].props.onValueChange('project-two');
  tree = h.render();
  assert.deepEqual(h.requests.at(-1), { teamId: undefined, projectId: 'project-two', offset: 0, limit: PAGE });
  assert.deepEqual(rangeLabel(tree), [1, 20, 45]);
  button(tree, h.t.common.next).props.onClick();
  tree = h.render();
  assert.equal(h.requests.at(-1).offset, 20);
  selects(tree)[1].props.onValueChange('team-two');
  tree = h.render();
  assert.deepEqual(h.requests.at(-1), { teamId: 'team-two', projectId: 'project-two', offset: 0, limit: PAGE });
  assert.equal(button(tree, h.t.common.previous).props.disabled, true);
});

test('a poll that removes the current last page returns to the nearest existing page', () => {
  let total = 45;
  const h = harness({ totals: () => total });
  let tree = h.render();
  button(tree, h.t.common.next).props.onClick();
  button(h.render(), h.t.common.next).props.onClick();
  h.render();
  assert.equal(h.requests.at(-1).offset, 40);
  // Five agents leave the scope: page 41-45 no longer exists.
  total = 40;
  h.render();
  tree = h.render();
  assert.equal(h.requests.at(-1).offset, 20);
  assert.deepEqual(rangeLabel(tree), [21, 40, 40]);
  assert.equal(button(tree, h.t.common.next).props.disabled, true);
  // Everything leaves: back to the first page with an empty range.
  total = 0;
  h.render();
  tree = h.render();
  assert.equal(h.requests.at(-1).offset, 0);
  assert.deepEqual(rangeLabel(tree), [0, 0, 0]);
});
