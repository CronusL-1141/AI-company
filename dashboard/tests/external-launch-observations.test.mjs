import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const ts = require('typescript');
const SRC = fileURLToPath(new URL('../src/', import.meta.url));
// Production-shaped rows; tests/unit/test_dashboard_fixture_contract.py
// validates every row against the API model and its full enum coverage.
const records = JSON.parse(readFileSync(new URL('./fixtures/codex-launch-observations.json', import.meta.url), 'utf8'));
const reasons = records.map((record) => record.binding_reason);

function harness({ lang = 'zh', data, error, loading = false } = {}) {
  let translations;
  let hookIndex = 0;
  const state = [];
  const queries = [];
  const calls = [];
  const mocks = {
    react: { ...React, useState: (initial) => {
      const index = hookIndex++;
      if (!(index in state)) state[index] = initial;
      return [state[index], (value) => { state[index] = value; }];
    } },
    '@/i18n': { useT: () => translations },
    '@tanstack/react-query': { useQuery: (query) => {
      queries.push(query);
      return { data: data ? { success: true, data } : undefined, error,
        isLoading: loading, isFetching: false };
    } },
    '@/api/client': { apiFetch: async (url) => { calls.push(url); return { success: true, data }; } },
    '@/components/ui/card': {},
    '@/components/ui/badge': { Badge: ({ children }) => React.createElement('span', {}, children) },
    '@/components/ui/button': { Button: ({ children, variant: _variant, size: _size, ...props }) => React.createElement('button', props, children) },
  };
  const cache = new Map();
  function load(path) {
    const absolute = ['', '.ts', '.tsx'].map((suffix) => resolve(SRC, path.replace(/^@\//, '')) + suffix).find(existsSync);
    assert.ok(absolute, path);
    if (cache.has(absolute)) return cache.get(absolute).exports;
    const module = { exports: {} };
    cache.set(absolute, module);
    const output = ts.transpileModule(readFileSync(absolute, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
    new Function('require', 'module', 'exports', output)((name) => {
      if (name in mocks) return mocks[name];
      if (name.startsWith('@/')) return load(name);
      if (name.startsWith('.')) {
        const path = resolve(dirname(absolute), name).slice(SRC.length);
        const alias = '@/' + path;
        return alias in mocks ? mocks[alias] : load(path);
      }
      return require(name);
    }, module, module.exports);
    return module.exports;
  }
  function expand(node) {
    if (node == null || typeof node !== 'object') return node;
    if (Array.isArray(node)) return node.map(expand);
    if (typeof node.type === 'function') return expand(node.type(node.props));
    return React.cloneElement(node, {}, ...React.Children.toArray(node.props.children).map(expand));
  }
  translations = lang === 'en' ? load('i18n/en.ts').en : load('i18n/zh.ts').zh;
  const Component = load('components/usage/ExternalExecPanel.tsx').CodexLaunchObservations;
  const render = () => { hookIndex = 0; return expand(React.createElement(Component)); };
  return { load, render, queries, calls, t: translations.externalExec,
    html: () => renderToStaticMarkup(render()) };
}

function nodes(tree, predicate) {
  const result = [];
  function visit(node) {
    if (node == null || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(visit); return; }
    if (predicate(node)) result.push(node);
    React.Children.forEach(node.props.children, visit);
  }
  visit(tree);
  return result;
}

const open = (h) => h.render().props.onToggle({ currentTarget: { open: true } });

test('launch records load only after expanding and use the existing usage refresh key without polling', async () => {
  const h = harness();
  assert.ok(h.html().includes(h.t.launchTitle));
  assert.ok(!h.html().includes(h.t.launchNotice));
  assert.equal(h.queries.at(-1).enabled, false);
  open(h);
  h.render();
  assert.equal(h.queries.at(-1).enabled, true);
  assert.deepEqual(h.queries.at(-1).queryKey, ['usage', 'codex-launch-observations', 0]);
  assert.equal(h.queries.at(-1).refetchInterval, undefined);
  await h.queries.at(-1).queryFn();
  assert.deepEqual(h.calls, ['/api/external-execs/launch-observations?limit=20&offset=0']);
  h.render().props.onToggle({ currentTarget: { open: false } });
  h.render();
  assert.equal(h.queries.at(-1).enabled, false);
});

test('launch record states, requested parameters and unbound reasons are explicit in both languages', () => {
  for (const lang of ['zh', 'en']) {
    const h = harness({ lang, data: { items: records, total: records.length, offset: 0, limit: 20, truncated: false } });
    open(h);
    const html = h.html();
    assert.ok(html.includes(h.t.launchNotice));
    assert.ok(html.includes('fast') && html.includes('gpt-6.1-sol') && html.includes('high'));
    for (const state of ['planned', 'unbound', 'bound', 'failed']) assert.ok(html.includes(h.t.launchStates[state]));
    for (const reason of reasons.filter((item) => item !== 'bound')) assert.ok(html.includes(h.t.launchReasons[reason]), reason);
    assert.ok(html.includes(`${h.t.launchThread}: native-thread`));
    assert.ok(!html.includes('thread: '));
    for (const mode of new Set(records.map((record) => record.exec_mode))) {
      assert.ok(html.includes(`· ${h.t.launchExecModes[mode]}</span>`), `${lang} ${mode}`);
      if (lang === 'zh') assert.ok(!html.includes(`· ${mode}</span>`), mode);
    }
    const bound = nodes(h.render(), (node) => node.type === 'li')
      .find((node) => renderToStaticMarkup(node).includes('native-thread'));
    assert.ok(renderToStaticMarkup(bound).includes(h.t.launchExecutionFailed));
    assert.ok(renderToStaticMarkup(bound).includes(h.t.launchStates.bound));
    assert.ok(!html.includes('service_tier_source') && !html.includes('command='));
  }
});

test('launch record pages preserve limits, return to the first page and obey the truncated flag', async () => {
  const h = harness({ data: { items: [records[0]], total: 21, offset: 0, limit: 20, truncated: true } });
  open(h);
  let buttons = nodes(h.render(), (node) => node.type === 'button');
  assert.equal(buttons[0].props.disabled, true);
  assert.equal(buttons[1].props.disabled, false);
  buttons[1].props.onClick();
  h.render();
  assert.equal(h.queries.at(-1).queryKey.at(-1), 20);
  await h.queries.at(-1).queryFn();
  assert.equal(h.calls.at(-1), '/api/external-execs/launch-observations?limit=20&offset=20');
  buttons = nodes(h.render(), (node) => node.type === 'button');
  assert.equal(buttons[0].props.disabled, false);
  buttons[0].props.onClick();
  h.render();
  assert.equal(h.queries.at(-1).queryKey.at(-1), 0);
  const last = harness({ data: { items: [], total: 0, offset: 0, limit: 20, truncated: false } });
  open(last);
  assert.equal(nodes(last.render(), (node) => node.type === 'button')[1].props.disabled, true);
  assert.ok(last.html().includes(last.t.launchEmpty));
});

test('launch failures remain errors and cached evidence remains visible instead of becoming an empty success', () => {
  const h = harness({ error: new Error('launch feed unavailable'), data: {
    items: [records[2]], total: 1, offset: 0, limit: 20, truncated: false,
  } });
  open(h);
  const html = h.html();
  assert.ok(html.includes('role="alert"') && html.includes('launch feed unavailable'));
  assert.ok(html.includes(h.t.launchReasons.background_or_redirect));
  assert.ok(!html.includes(h.t.launchEmpty));
});
