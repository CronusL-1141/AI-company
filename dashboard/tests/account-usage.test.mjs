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
const KEY = 'a'.repeat(64);
const account = { account_key: KEY, label: 'Work account', created_at: '2026-09-14T09:00:00Z' };
const start = { snapshot_id: 'start', account_key: KEY, limit_id: 'weekly', used_percent: '10',
  window_duration_ms: 604800000, resets_at: '2026-09-20T00:00:00Z',
  observed_at: '2026-09-14T10:00:00Z', source: 'codex_app_server' };
const end = { ...start, snapshot_id: 'end', used_percent: '12', observed_at: '2026-09-14T11:00:00Z' };
const entry = { occurred_at: '2026-09-14T10:30:00Z', request: { request_id: 'req-one', model: 'model-one',
  service_tier: 'standard', input_tokens: 100, output_tokens: 20, cached_input_tokens: 30, cache_write_input_tokens: 0 } };
const raw = JSON.stringify([entry]);
const quote = { basis: 'api_equivalent_at_catalog_version', currency: 'USD', catalog_version: 'test-v1',
  catalog_sha256: 'b'.repeat(64), verified_at: '2026-09-14T09:00:00Z', request_count: 1,
  priced_request_count: 1, unpriced_request_count: 0, complete: true, total_usd: '1.2300',
  priced_subtotal_usd: '1.2300', items: [], missing_models: [] };
const estimate = { batch_id: 'batch-one', account_key: KEY, start_snapshot_id: 'start', end_snapshot_id: 'end',
  coverage: 'local_only', coverage_statement: '', coverage_confirmed_at: null,
  interval_start: start.observed_at, interval_end: end.observed_at, delta_used_percent: '2', quote,
  estimated_full_week_usd: null, status: 'sample_only', reason: '仅本地样本，未外推。' };
const planEstimate = { account_key: KEY, limit_id: 'codex', window_duration_ms: 604800000,
  resets_at: start.resets_at, observed_at: end.observed_at, used_percent: 12,
  estimated_total_tokens: 12300000, delta_tokens: 246000, delta_used_percent: 2,
  start_snapshot_id: start.snapshot_id, end_snapshot_id: end.snapshot_id,
  interval_start: start.observed_at, status: 'estimated', source: 'codex_local_logs' };
const monitorState = { account_key: KEY, settings: { enabled: false, interval_ms: 300000 }, revision: 0,
  status: 'disabled', runtime_running: false, last_started_at: null, last_finished_at: null,
  next_run_at: null, last_error: null };

function harness(overrides = {}) {
  const cache = new Map();
  const slots = new Map();
  const hookIndexes = new Map();
  let activePath = '';
  let translations;
  const calls = [];
  const queries = [];
  const mutations = [];
  const invalidations = [];
  const detailReads = [];
  const state = { accounts: [account], detail: { account, snapshots: [start, end], estimates: [], plan_estimates: [planEstimate] },
    monitor: structuredClone(monitorState), cachedMonitor: undefined, ...overrides };
  function useSlot(initial) {
    const path = activePath;
    const index = hookIndexes.get(path) || 0;
    hookIndexes.set(path, index + 1);
    const values = slots.get(path) || [];
    if (!(index in values)) values[index] = typeof initial === 'function' ? initial() : initial;
    slots.set(path, values);
    return [values[index], (next) => { values[index] = typeof next === 'function' ? next(values[index]) : next; }];
  }
  const hooks = {
    useQueryClient: () => ({ invalidateQueries: async (query) => { invalidations.push(query); },
      getQueryData: () => state.cachedMonitor,
      setQueryData: (_key, data) => { state.cachedMonitor = data; } }),
    useQuery: (options) => { queries.push(options); return {}; },
    useMutation: (options) => { mutations.push(options); return options; },
  };
  const emptyMutation = { isPending: false, isError: false, isSuccess: false };
  const apiMock = {
    usePricingAccounts: () => ({ data: { accounts: state.accounts }, isError: false, isLoading: false, isFetching: false }),
    usePricingAccount: (key, includePricing = true) => {
      detailReads.push({ key, includePricing });
      return { data: state.detail, isError: false, isLoading: false, isFetching: false };
    },
    useCapturePricingAccount: () => ({ ...emptyMutation, mutate: (_input, options) => {
      calls.push({ capture: true }); options.onSuccess({ account, snapshots: [start, end] });
    } }),
    useLabelPricingAccount: () => ({ ...emptyMutation, mutateAsync: async (input) => {
      calls.push(input);
      if (state.labelError) throw new Error(state.labelError);
      return { ...account, label: input.label };
    } }),
    useImportPricingBatch: () => ({ ...emptyMutation, mutateAsync: async (input) => {
      calls.push(input);
      if (state.saveError) throw new Error(state.saveError);
      return { ...estimate, batch_id: input.batch_id, coverage: input.coverage };
    } }),
    usePricingMonitor: () => ({ data: state.monitor, isError: false, isLoading: false }),
    useUpdatePricingMonitor: () => ({ ...emptyMutation, mutateAsync: async (input) => {
      calls.push({ monitor: input });
      if (state.monitorError) throw new Error(state.monitorError);
      state.monitor = { ...state.monitor, settings: input.settings, revision: state.monitor.revision + 1,
        status: input.settings.enabled ? 'waiting' : 'disabled' };
      return state.monitor;
    } }),
  };
  const mocks = {
    react: { ...React, useState: useSlot, useRef: (initial) => useSlot({ current: initial })[0],
      useReducer: (reducer, initial, init) => {
        const [value, setValue] = useSlot(() => init ? init(initial) : initial);
        return [value, (action) => setValue((previous) => reducer(previous, action))];
      } },
    'lucide-react': { RefreshCw: () => null, ScanLine: () => null },
    '@tanstack/react-query': hooks,
    '@/i18n': { useT: () => translations },
    '@/api/accountUsage': apiMock,
    '@/api/planUsage': { usePlanCapacity: () => ({ data: state.detail?.plan_estimates ?? [], isLoading: false, isError: false }) },
    '@/api/client': { apiFetch: async (path, options) => {
      calls.push({ path, options }); return { success: true, data: path.endsWith('/monitor') ? state.monitor : {} };
    } },
    '@/components/ui/button': { Button: ({ children, ...props }) => React.createElement('button', props, children) },
    '@/components/ui/input': { Input: (props) => React.createElement('input', props) },
  };
  function load(path) {
    let absolute = path.startsWith('@/') ? resolve(SRC, path.slice(2)) : resolve(SRC, path);
    absolute = ['', '.ts', '.tsx', '/index.ts'].map((suffix) => absolute + suffix).find(existsSync);
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
        const target = resolve(dirname(absolute), name);
        const alias = '@/' + target.slice(SRC.length);
        return alias in mocks ? mocks[alias] : load(target);
      }
      return require(name);
    }, module, module.exports);
    return module.exports;
  }
  function expand(element, path = 'root') {
    if (element == null || typeof element !== 'object') return element;
    if (Array.isArray(element)) return element.map((child, index) => expand(child, `${path}/${child?.key ?? index}`));
    if (typeof element.type === 'function') {
      activePath = path; hookIndexes.set(path, 0);
      return expand(element.type(element.props), `${path}/body`);
    }
    const children = React.Children.toArray(element.props.children);
    const props = { ...element.props, key: element.key };
    delete props.children;
    return React.createElement(element.type, props,
      ...children.map((child, index) => expand(child, `${path}/${child?.key ?? index}`)));
  }
  translations = state.lang === 'en' ? load('i18n/en.ts').en : load('i18n/zh.ts').zh;
  const page = load('pages/AccountUsagePage.tsx').AccountUsagePage;
  return { load, state, calls, queries, mutations, invalidations, detailReads, t: translations.accountUsage,
    render: () => expand(React.createElement(page)), html: () => renderToStaticMarkup(expand(React.createElement(page))) };
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

test('parser preserves complete request data and rejects duplicate IDs, extra data, unsafe counts and absent timezone', () => {
  const { parseAccountEntries } = harness().load('lib/account-usage.ts');
  assert.deepEqual(parseAccountEntries(raw), [entry]);
  assert.throws(() => parseAccountEntries(JSON.stringify([entry, entry])));
  assert.throws(() => parseAccountEntries(JSON.stringify([{ ...entry, credential: 'never-send' }])));
  assert.throws(() => parseAccountEntries(JSON.stringify([{ ...entry, occurred_at: '2026-09-14T10:30:00' }])));
  for (const value of [-1, 1.5, Number.MAX_SAFE_INTEGER + 1, '100']) {
    assert.throws(() => parseAccountEntries(JSON.stringify([{ ...entry, request: { ...entry.request, input_tokens: value } }])));
  }
  assert.throws(() => parseAccountEntries(JSON.stringify([{ ...entry, request: { ...entry.request, cached_input_tokens: 101 } }])));
  assert.throws(() => parseAccountEntries(JSON.stringify([{ ...entry, request: { ...entry.request, service_tier: 'invented' } }])));
  assert.throws(() => parseAccountEntries('[]'));
});

test('confirmation invalidates when any form input, account or snapshot content changes', () => {
  const lib = harness().load('lib/account-usage.ts');
  const draft = { ...lib.initialAccountImport(KEY), startId: 'start', endId: 'end', raw, statement: 'Checked all clients' };
  const confirmed = lib.accountImportReducer(draft, { type: 'confirm', key: lib.importConfirmationKey(draft, start, end), at: '2026-09-14T11:01:00Z' });
  assert.equal(lib.buildAccountBatch(confirmed, start, end, 'batch').coverage, 'account_complete');
  for (const field of ['startId', 'endId', 'raw', 'statement']) {
    const edited = lib.accountImportReducer(confirmed, { type: 'edit', field, value: confirmed[field] });
    assert.equal(edited.confirmationKey, null);
    assert.equal(edited.confirmedAt, null);
  }
  assert.equal(lib.buildAccountBatch(confirmed, start, { ...end, used_percent: '13' }, 'batch').coverage, 'local_only');
  assert.throws(() => lib.buildAccountBatch({ ...confirmed, accountKey: 'b'.repeat(64) }, start, end, 'batch'));
});

test('JSON duplicate keys are rejected before decoding, including nested and escaped spellings', () => {
  const { parseAccountEntries } = harness().load('lib/account-usage.ts');
  assert.throws(() => parseAccountEntries(raw.replace('"input_tokens":100', '"input_tokens":100,"input_tokens":200')));
  assert.throws(() => parseAccountEntries(raw.replace('"request":{', '"request":{},"request":{')));
  assert.throws(() => parseAccountEntries(raw.replace('"input_tokens":100', '"input_tokens":100,"input_\\u0074okens":200')));
  assert.throws(() => parseAccountEntries(raw.replace('"request":{', '"re\\u0071uest":{},"request":{')));
  const quoted = { ...entry, request: { ...entry.request, request_id: 'escaped-"quote-\\slash-{key:1}' } };
  assert.deepEqual(parseAccountEntries(JSON.stringify([quoted])), [quoted]);
  assert.deepEqual(parseAccountEntries(JSON.stringify([entry, { ...entry, request: { ...entry.request, request_id: 'req-two' } }])),
    [entry, { ...entry, request: { ...entry.request, request_id: 'req-two' } }]);
  for (const invalid of ['[', '{} garbage', '[{}', '[{"a":1,}]', '[[[]]']) {
    assert.throws(() => parseAccountEntries(invalid));
  }
});

test('import interval excludes its start, includes its end and rejects other buckets or reset cycles', () => {
  const lib = harness().load('lib/account-usage.ts');
  const draft = { ...lib.initialAccountImport(KEY), raw, startId: 'start', endId: 'end' };
  assert.equal(lib.buildAccountBatch(draft, start, end, 'stable-retry').batch_id, 'stable-retry');
  assert.equal(lib.buildAccountBatch(draft, start, end, 'stable-retry').coverage_confirmed_at, null);
  const boundary = (at) => ({ ...draft, raw: JSON.stringify([{ ...entry, occurred_at: at }]) });
  assert.throws(() => lib.buildAccountBatch(boundary(start.observed_at), start, end, 'batch'));
  assert.doesNotThrow(() => lib.buildAccountBatch(boundary(end.observed_at), start, end, 'batch'));
  for (const changed of [{ limit_id: 'other' }, { resets_at: '2026-09-21T00:00:00Z' }, { window_duration_ms: 18000000 }]) {
    assert.equal(lib.compatibleSnapshotPair(start, { ...end, ...changed }), false);
  }
});

test('saved drafts clear content only after success', () => {
  const lib = harness().load('lib/account-usage.ts');
  const draft = { ...lib.initialAccountImport(KEY), raw, startId: 'start', endId: 'end', statement: 'basis' };
  const saved = lib.accountImportReducer(draft, { type: 'saved' });
  assert.equal(saved.raw, '');
  assert.equal(saved.statement, '');
  assert.equal(saved.startId, 'start');
});

test('empty account page has a real capture action without fabricated quota or OAuth', () => {
  const h = harness({ accounts: [], detail: undefined });
  const html = h.html();
  assert.ok(html.includes(h.t.noAccounts));
  assert.ok(!html.includes('0%'));
  const capture = nodes(h.render(), (node) => node.type === 'button').at(-1);
  capture.props.onClick();
  assert.deepEqual(h.calls, [{ capture: true }]);
});

test('plan cards show only compact Token capacity and usage, without price or import forms', () => {
  const h = harness({ detail: { account, snapshots: [start, end], estimates: [estimate], plan_estimates: [planEstimate] } });
  const tree = h.render();
  const html = renderToStaticMarkup(tree);
  const articles = nodes(tree, (node) => node.type === 'article');
  assert.equal(articles.length, 1);
  const labels = nodes(articles[0], (node) => node.type === 'dt').map((node) => node.props.children);
  assert.deepEqual(labels, [h.t.planCapacity, h.t.planUsed]);
  assert.ok(html.includes('12.3M'));
  assert.ok(html.includes('Token'));
  assert.ok(html.includes('12%'));
  assert.ok(!html.includes('USD'));
  assert.ok(!html.includes(h.t.sampleCost));
  assert.equal(nodes(tree, (node) => node.type === 'textarea' || (node.type === 'input' && node.props.type === 'file')).length, 0);
  const settings = nodes(tree, (node) => node.type === 'details')[0];
  assert.ok(settings);
  assert.ok(!settings.props.open);
  assert.ok(html.includes(h.t.accountSettings));
});

test('plan status placeholders preserve unknown versus zero and hide expired percentages', () => {
  for (const [status, text, used] of [
    ['collecting', 'planCollecting', 25], ['unavailable', 'planUnavailable', null], ['expired', 'planExpired', 25],
  ]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [{
      ...planEstimate, estimated_total_tokens: null, used_percent: used,
      delta_tokens: null, delta_used_percent: null, start_snapshot_id: null, interval_start: null, status,
    }] } });
    const article = nodes(h.render(), (node) => node.type === 'article')[0];
    const html = renderToStaticMarkup(article);
    assert.ok(html.includes(h.t[text]));
    assert.ok(!html.includes('0%'));
    if (status === 'collecting') assert.ok(html.includes('25%'));
    if (status === 'expired') assert.ok(!html.includes('25%'));
  }
  const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [{
    ...planEstimate, status: 'unavailable', used_percent: 0, estimated_total_tokens: null,
    delta_tokens: null, delta_used_percent: null, start_snapshot_id: null, interval_start: null,
  }] } });
  assert.ok(h.html().includes('0%'));
});

test('missing aligned activity is visible in both languages while the real percentage remains', () => {
  for (const [lang, expected] of [['zh', '缺少同步用量数据'], ['en', 'Aligned usage unavailable']]) {
    const h = harness({ lang, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
      ...planEstimate, source: 'codex_account_activity', status: 'unavailable', reason_code: 'activity_coverage_unknown',
      estimated_total_tokens: null, delta_tokens: null, delta_used_percent: null,
      start_snapshot_id: null, interval_start: null, used_percent: 37,
    }] } });
    const article = nodes(h.render(), (node) => node.type === 'article')[0];
    const values = nodes(article, (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
    assert.equal(values.length, 2);
    assert.ok(values[0].includes(expected));
    assert.ok(!values[0].includes(h.t.planCollecting));
    assert.ok(!values[0].includes('Token'));
    assert.ok(values[1].includes('37%'));
  }
});

test('unattributed bucket keeps no-data text and its real zero percentage', () => {
  const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [{
    ...planEstimate, status: 'unavailable', reason_code: 'bucket_activity_unattributed',
    estimated_total_tokens: null, delta_tokens: null, delta_used_percent: null,
    start_snapshot_id: null, interval_start: null, used_percent: 0,
  }] } });
  const article = nodes(h.render(), (node) => node.type === 'article')[0];
  const values = nodes(article, (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
  assert.ok(values[0].includes(h.t.planUnavailable));
  assert.ok(values[1].includes('0%'));
});

test('contradictory estimated responses with a valid rejection reason never display capacity', () => {
  // Exercise inconsistent data without inventing status or reason enum members.
  for (const [reason, expected] of [
    ['activity_coverage_unknown', '缺少同步用量数据'],
    ['bucket_activity_unattributed', '暂无数据'],
    ['local_usage_unavailable', '本机日志暂不可用'],
  ]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [{
      ...planEstimate, reason_code: reason,
    }] } });
    const article = nodes(h.render(), (node) => node.type === 'article')[0];
    const values = nodes(article, (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
    assert.ok(values[0].includes(expected));
    assert.ok(!values[0].includes('12.3M'));
    assert.ok(!values[0].includes('Token'));
    assert.ok(values[1].includes('12%'));
  }
});

test('local estimates without a reason and explicit null reasons retain their capacity', () => {
  for (const item of [planEstimate, { ...planEstimate, reason_code: null }]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [item] } });
    const article = nodes(h.render(), (node) => node.type === 'article')[0];
    const html = renderToStaticMarkup(article);
    assert.ok(html.includes('12.3M'));
    assert.ok(html.includes('12%'));
  }
});

test('local estimated and collecting cards carry the short source label with the real percentage', () => {
  for (const [lang, label] of [['zh', '本机样本估算'], ['en', 'Local-sample estimate']]) {
    for (const status of ['estimated', 'collecting']) {
      const item = status === 'estimated' ? { ...planEstimate, used_percent: 37 } : {
        ...planEstimate, status, used_percent: 37, estimated_total_tokens: null,
        delta_tokens: null, delta_used_percent: null, start_snapshot_id: null, interval_start: null,
      };
      const h = harness({ lang, detail: { account, snapshots: [], estimates: [], plan_estimates: [item] } });
      const article = nodes(h.render(), (node) => node.type === 'article')[0];
      const html = renderToStaticMarkup(article);
      const values = nodes(article, (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
      assert.equal(values.length, 2);
      assert.ok(html.includes(label));
      assert.ok(values[1].includes('37%'));
      if (status === 'estimated') assert.ok(values[0].includes('12.3M'));
      else {
        assert.ok(values[0].includes(h.t.planCollecting));
        assert.ok(!values[0].includes('12.3M'));
      }
    }
  }
});

test('unavailable local logs show their short reason without replacing the observed percentage', () => {
  for (const [lang, expected] of [['zh', '本机日志暂不可用'], ['en', 'Local logs unavailable']]) {
    const h = harness({ lang, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
      ...planEstimate, status: 'unavailable', reason_code: 'local_usage_unavailable',
      estimated_total_tokens: null, delta_tokens: null, delta_used_percent: null,
      start_snapshot_id: null, interval_start: null, used_percent: 41,
    }] } });
    const article = nodes(h.render(), (node) => node.type === 'article')[0];
    const values = nodes(article, (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
    assert.ok(values[0].includes(expected));
    assert.ok(!values[0].includes(h.t.planCollecting));
    assert.ok(!values[0].includes('Token'));
    assert.ok(values[1].includes('41%'));
  }
});

test('legacy activity source cannot revive a cached estimate or imply an eligible sampling window', () => {
  for (const changes of [{}, { reason_code: null }, { status: 'collecting', estimated_total_tokens: null }]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [{
      ...planEstimate, ...changes, source: 'codex_account_activity',
    }] } });
    const article = nodes(h.render(), (node) => node.type === 'article')[0];
    const html = renderToStaticMarkup(article);
    const values = nodes(article, (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
    assert.ok(values[0].includes(h.t.planUnavailable));
    assert.ok(!values[0].includes('12.3M'));
    assert.ok(!values[0].includes(h.t.planCollecting));
    assert.ok(!html.includes(h.t.planLocalSampleEstimate));
    assert.ok(values[1].includes('12%'));
  }
});

test('plan cards abbreviate large valid counts and reject unsafe values without dollar arithmetic', () => {
  for (const [value, expected] of [[999, '999'], [1000, '1K'], [12300000, '12.3M'], [2500000000, '2.5B'], [9000000000000, '9T']]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [{
      ...planEstimate, estimated_total_tokens: value,
    }] } });
    const html = renderToStaticMarkup(nodes(h.render(), (node) => node.type === 'article')[0]);
    assert.ok(html.includes(expected), html);
    assert.ok(html.includes('native_activity'));
  }
  const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [{
    ...planEstimate, estimated_total_tokens: Number.MAX_SAFE_INTEGER + 1,
  }] } });
  const html = renderToStaticMarkup(nodes(h.render(), (node) => node.type === 'article')[0]);
  assert.ok(html.includes(h.t.planUnavailable));
});

test('every plan bucket has its own window and the page reads the latest capacity result', () => {
  const second = { ...planEstimate, limit_id: 'short-bucket', window_duration_ms: 18000000, estimated_total_tokens: 2500000000 };
  const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [planEstimate, second] } });
  const articles = nodes(h.render(), (node) => node.type === 'article').map((node) => renderToStaticMarkup(node));
  assert.equal(articles.length, 2);
  assert.ok(articles[0].includes('12.3M'));
  assert.ok(articles[0].includes(h.t.planWeek));
  assert.ok(articles[1].includes('short-bucket'));
  assert.ok(articles[1].includes('2.5B'));
  assert.ok(articles[1].includes(h.t.planHours(5)));
  h.state.detail.plan_estimates = [{ ...planEstimate, estimated_total_tokens: 99000000 }];
  assert.ok(h.html().includes('99M'));
  assert.ok(!h.html().includes('12.3M'));
});

test('plan results never mix another account into the selected account card', () => {
  const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [
    planEstimate, { ...planEstimate, account_key: 'b'.repeat(64), estimated_total_tokens: 999000000 },
  ] } });
  assert.equal(nodes(h.render(), (node) => node.type === 'article').length, 1);
  assert.ok(!h.html().includes('999M'));
});

test('plan page and selector skip pricing and share a cache separate from legacy details', async () => {
  const h = harness();
  h.render();
  assert.deepEqual(h.detailReads, [{ key: KEY, includePricing: false }]);
  h.load('api/accountUsage.ts').usePricingAccount(KEY);
  const legacyQuery = h.queries.at(-1);
  h.load('api/accountUsage.ts').usePricingAccount(KEY, false);
  const accountQuery = h.queries.at(-1);
  h.load('api/planUsage.ts').usePlanCapacity(KEY);
  const planQuery = h.queries.at(-1);
  assert.deepEqual(planQuery.queryKey, accountQuery.queryKey);
  assert.notDeepEqual(planQuery.queryKey, legacyQuery.queryKey);
  assert.deepEqual(planQuery.queryKey, ['account-usage', KEY, 'plan']);
  assert.equal(planQuery.refetchInterval, undefined);
  await legacyQuery.queryFn();
  assert.equal(h.calls.at(-1).path, '/api/account-usage/' + KEY);
  await accountQuery.queryFn();
  assert.equal(h.calls.at(-1).path, '/api/account-usage/' + KEY + '?include_pricing=false');
  await planQuery.queryFn();
  assert.equal(h.calls.at(-1).path, '/api/account-usage/' + KEY + '?include_pricing=false');
  assert.deepEqual(planQuery.select({ ...h.state.detail, plan_estimates: [planEstimate] }), [planEstimate]);
  assert.deepEqual(planQuery.select({ account, snapshots: [], estimates: [] }), []);
});

test('account alias editing remains available and failure preserves the draft', async () => {
  const h = harness({ labelError: '保存失败，请重试' });
  const field = () => nodes(h.render(), (node) => node.type === 'input' && !node.props.type)[0];
  field().props.onChange({ target: { value: 'Personal Codex' } });
  const save = () => nodes(h.render(), (node) => node.type === 'form' && node.props['aria-label'] === h.t.label)[0].props.onSubmit({ preventDefault() {} });
  await save();
  assert.equal(field().props.value, 'Personal Codex');
  assert.ok(h.html().includes('保存失败，请重试'));
  h.state.labelError = null;
  await save();
  assert.equal(h.calls.at(-1).label, 'Personal Codex');
  assert.ok(h.html().includes(h.t.labelSaved));
});

test('read hooks do not poll or capture, and capture uses the explicit mutation route', async () => {
  const h = harness();
  const api = h.load('api/accountUsage.ts');
  api.usePricingAccounts(); api.usePricingAccount(KEY);
  assert.equal(h.calls.length, 0);
  for (const query of h.queries) {
    assert.equal(query.refetchOnWindowFocus, false);
    assert.equal(query.refetchInterval, undefined);
    await query.queryFn();
  }
  api.useCapturePricingAccount();
  await h.mutations.at(-1).mutationFn();
  assert.deepEqual(h.calls.at(-1), { path: '/api/account-usage/capture', options: { method: 'POST', body: '{}' } });
  api.useImportPricingBatch();
  await h.mutations.at(-1).mutationFn({ batch_id: 'batch', account_key: KEY, entries: [] });
  assert.equal(h.calls.at(-1).path, `/api/account-usage/${KEY}/batches`);
});

test('monitor intervals only accept whole minutes from 5 through 1440', () => {
  const { monitorIntervalMilliseconds } = harness().load('lib/account-usage.ts');
  assert.equal(monitorIntervalMilliseconds('5'), 300000);
  assert.equal(monitorIntervalMilliseconds('1440'), 86400000);
  for (const value of ['', '4', '1441', '5.5', 'NaN', '-5', '1e3']) {
    assert.equal(monitorIntervalMilliseconds(value), null);
  }
});

test('epoch millisecond conversion preserves UTC and rejects invalid or nonnumeric input', () => {
  const { epochMsToIso } = harness().load('lib/datetime.ts');
  assert.equal(epochMsToIso(0), '1970-01-01T00:00:00.000Z');
  assert.equal(epochMsToIso(-1), '1969-12-31T23:59:59.999Z');
  assert.equal(epochMsToIso(Date.UTC(2026, 8, 14, 10, 5)), '2026-09-14T10:05:00.000Z');
  for (const value of [NaN, Infinity, -Infinity, 8.64e15 + 1, -8.64e15 - 1, null, undefined, '0']) {
    assert.equal(epochMsToIso(value), null);
  }
});

test('monitor is off by default and enabling only reports saved configuration until sampling occurs', async () => {
  const h = harness();
  const initial = h.html();
  assert.ok(initial.includes(h.t.monitorDisabled));
  assert.ok(initial.includes(h.t.monitorRuntimeStopped));
  assert.ok(initial.includes(h.t.monitorSingleSource));
  assert.equal(h.calls.length, 0);
  const field = nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0];
  field.props.onChange({ target: { value: '15' } });
  const form = nodes(h.render(), (node) => node.type === 'form' && node.props['aria-label'] === h.t.monitorTitle)[0];
  form.props.onSubmit({ preventDefault() {} });
  await new Promise(setImmediate);
  assert.deepEqual(h.calls.at(-1), { monitor: { key: KEY, settings: { enabled: true, interval_ms: 900000 } } });
  assert.ok(h.html().includes(h.t.monitorSaved));
  assert.ok(h.html().includes(h.t.monitorWaiting));
  assert.ok(h.html().includes(h.t.monitorRuntimeStopped));
  assert.ok(!h.html().includes(h.t.captured));
  assert.equal(h.state.monitor.last_finished_at, null);
});

test('monitor conflicts preserve the interval and paused-account state is explicit', async () => {
  const h = harness({ monitorError: '请先暂停已有账号监控', monitor: { ...monitorState,
    status: 'paused_account_changed', last_error: '本机账号已切换' } });
  assert.ok(h.html().includes(h.t.monitorAccountChanged));
  const field = () => nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0];
  field().props.onChange({ target: { value: '30' } });
  nodes(h.render(), (node) => node.type === 'form' && node.props['aria-label'] === h.t.monitorTitle)[0].props.onSubmit({ preventDefault() {} });
  await new Promise(setImmediate);
  assert.equal(field().props.value, '30');
  assert.equal(h.state.monitor.settings.enabled, false);
  assert.ok(h.html().includes('请先暂停已有账号监控'));
});

test('pause submits only disabled settings and preserves the saved interval', async () => {
  const h = harness({ monitor: { ...monitorState, settings: { enabled: true, interval_ms: 600000 },
    status: 'waiting', runtime_running: true } });
  nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0].props.onChange({ target: { value: '' } });
  const pause = nodes(h.render(), (node) => node.type === 'button' && node.props.children === h.t.monitorPause)[0];
  pause.props.onClick(); await new Promise(setImmediate);
  assert.deepEqual(h.calls.at(-1), { monitor: { key: KEY, settings: { enabled: false, interval_ms: 600000 } } });
  assert.ok(h.html().includes(h.t.monitorPaused));
  assert.equal(h.calls.some((call) => call.batch_id || call.capture), false);
});

test('monitor polling is a lightweight GET and refreshes detail only after a new completion', async () => {
  const h = harness();
  const api = h.load('api/accountUsage.ts');
  api.usePricingMonitor(KEY);
  const query = h.queries.at(-1);
  assert.equal(query.refetchInterval, 10000);
  await query.queryFn();
  assert.equal(h.invalidations.length, 0);
  h.state.cachedMonitor = structuredClone(h.state.monitor);
  h.state.monitor.last_finished_at = '2026-09-14T11:05:00Z';
  await query.queryFn();
  assert.deepEqual(h.invalidations, [
    { queryKey: ['account-usage', KEY], exact: true },
    { queryKey: ['account-usage', KEY, 'plan'], exact: true },
  ]);
  h.state.cachedMonitor = structuredClone(h.state.monitor);
  await query.queryFn();
  assert.equal(h.invalidations.length, 2);
  assert.ok(h.calls.every((call) => call.path === `/api/account-usage/${KEY}/monitor` && !call.options));
  api.useUpdatePricingMonitor();
  const mutation = h.mutations.at(-1);
  const input = { key: KEY, settings: { enabled: true, interval_ms: 300000 } };
  await mutation.mutationFn(input);
  assert.equal(h.calls.at(-1).options.method, 'PUT');
  assert.deepEqual(JSON.parse(h.calls.at(-1).options.body), input.settings);
});

test('quota trends keep all account, bucket, reset and duration groups separate', () => {
  const { quotaTrends } = harness().load('lib/account-usage.ts');
  const now = Date.parse(end.observed_at);
  const secondary = [start, end].map((item) => ({ ...item, limit_id: 'secondary', snapshot_id: `secondary-${item.snapshot_id}` }));
  const otherWindow = [start, end].map((item) => ({ ...item, window_duration_ms: 18000000,
    resets_at: '2026-09-14T14:00:00Z', snapshot_id: `short-${item.snapshot_id}` }));
  const otherAccount = [start, end].map((item) => ({ ...item, account_key: 'b'.repeat(64), snapshot_id: `other-${item.snapshot_id}` }));
  const otherReset = [{ ...start, resets_at: '2026-09-13T00:00:00Z', snapshot_id: 'old-reset' }];
  const rows = quotaTrends([start, end, ...secondary, ...otherWindow, ...otherAccount, ...otherReset], now);
  assert.equal(rows.length, 5);
  assert.equal(rows.filter((row) => row.limit_id === 'secondary').length, 1);
  assert.equal(rows.filter((row) => row.account_key !== KEY).length, 1);
  assert.equal(rows.find((row) => row.end_snapshot_id === 'old-reset').status, 'expired');
});

test('a monitor completion arriving with a settings response also refreshes its account detail', () => {
  const h = harness({ cachedMonitor: structuredClone(monitorState) });
  h.load('api/accountUsage.ts').useUpdatePricingMonitor();
  const result = { ...monitorState, last_finished_at: '2026-09-14T11:05:00Z' };
  h.mutations.at(-1).onSuccess(result, { key: KEY });
  assert.deepEqual(h.invalidations, [
    { queryKey: ['account-usage', KEY], exact: true },
    { queryKey: ['account-usage', KEY, 'plan'], exact: true },
    { queryKey: ['account-usage', KEY, 'monitor'], exact: true },
  ]);
  assert.equal(h.state.cachedMonitor, result);
});

test('quota forecasts require five minutes and more than one percentage point, without filling missing data', () => {
  const { quotaTrends } = harness().load('lib/account-usage.ts');
  const check = (a, b) => quotaTrends([a, b], Date.parse('2026-09-14T12:00:00Z'))[0];
  assert.equal(check(start, { ...end, observed_at: '2026-09-14T10:04:59Z' }).status, 'insufficient');
  assert.equal(check(start, { ...end, used_percent: '11' }).estimated_exhaustion_at, null);
  assert.equal(check(start, { ...end, used_percent: null }).status, 'insufficient');
  assert.equal(check({ ...start, used_percent: '30' }, end).status, 'insufficient');
  assert.equal(check(start, { ...end, used_percent: '100' }).status, 'exhausted');
  const enough = check(start, { ...end, observed_at: '2026-09-14T10:05:00Z' });
  assert.equal(enough.status, 'before_reset');
  assert.equal(enough.interval_ms, 300000);
  assert.equal(enough.delta_used_percent, 2);
  assert.equal(enough.estimated_exhaustion_at, '2026-09-14T13:45:00.000Z');
});

test('a forecast exactly at reset reports reset first and every bucket remains visible', () => {
  const { quotaTrends } = harness().load('lib/account-usage.ts');
  const a = { ...start, used_percent: '10', window_duration_ms: 18000000, resets_at: '2026-09-14T15:00:00Z' };
  const b = { ...end, used_percent: '13', window_duration_ms: 18000000,
    resets_at: a.resets_at, observed_at: '2026-09-14T10:10:00Z' };
  const trend = quotaTrends([a, b], Date.parse(b.observed_at))[0];
  assert.equal(trend.status, 'reset_first');
  assert.equal(trend.estimated_exhaustion_at, '2026-09-14T15:00:00.000Z');
  assert.equal(trend.limit_id, a.limit_id);
});
