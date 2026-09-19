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
const pricingPlanEstimate = { account_key: KEY, limit_id: 'codex', window_duration_ms: 604800000,
  resets_at: start.resets_at, observed_at: end.observed_at, used_percent: 12,
  estimated_total_usd: '1234.56', delta_usd: '12.3456', delta_used_percent: 1,
  start_snapshot_id: start.snapshot_id, end_snapshot_id: end.snapshot_id,
  interval_start: start.observed_at, status: 'estimated', source: 'codex_local_logs',
  pricing_mode: 'standard_equivalent', catalog_version: 'test-v1', catalog_sha256: 'b'.repeat(64), reason_code: null };
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
  const state = { accounts: [account], detail: { account, snapshots: [start, end], estimates: [], plan_estimates: [planEstimate], pricing_plan_estimates: [pricingPlanEstimate] },
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
    useQueryClient: () => ({ invalidateQueries: async (query) => {
      invalidations.push(query);
      if (state.invalidationWait) await state.invalidationWait;
      if (state.resetDetail && query.queryKey[2] === 'plan') state.detail = state.resetDetail;
    },
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
      if (input.settings.interval_ms !== undefined && (!Number.isInteger(input.settings.interval_ms)
        || input.settings.interval_ms < 30000 || input.settings.interval_ms > 1800000)) {
        throw new Error('采样周期超出请求范围');
      }
      const settings = { enabled: input.settings.enabled,
        interval_ms: input.settings.interval_ms ?? state.monitor.settings.interval_ms };
      state.monitor = { ...state.monitor, settings, revision: state.monitor.revision + 1,
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
    '@/api/pricingPlanUsage': {
      usePricingPlanCapacity: () => ({ data: state.detail, isLoading: false, isError: false }),
      useResetPricingPlanAnchor: (key) => {
        const mutation = load('api/pricingPlanUsage.ts').useResetPricingPlanAnchor(key);
        return { ...emptyMutation, mutateAsync: async (input) => {
          const result = await mutation.mutationFn(input);
          await mutation.onSuccess(result);
          return result;
        } };
      },
    },
    '@/api/client': { apiFetch: async (path, options) => {
      calls.push({ path, options });
      if (path.endsWith('/plan-anchor/reset')) {
        if (state.resetWait) await state.resetWait;
        if (state.resetError) throw new Error(state.resetError);
        return { success: true, data: state.resetResult ?? {} };
      }
      return { success: true, data: path.endsWith('/monitor') ? state.monitor : {} };
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
  const page = state.tokenView ? load('components/usage/PlanCapacityPanel.tsx').PlanCapacityPanel : load('pages/AccountUsagePage.tsx').AccountUsagePage;
  return { load, state, calls, queries, mutations, invalidations, detailReads, t: translations.accountUsage,
    render: () => expand(React.createElement(page, state.tokenView ? { accountKey: KEY } : {})), html: () => renderToStaticMarkup(expand(React.createElement(page, state.tokenView ? { accountKey: KEY } : {}))) };
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

function currentPricingPlan(changes = {}) {
  const now = Date.now();
  return { ...pricingPlanEstimate, observed_at: new Date(now - 60000).toISOString(),
    interval_start: new Date(now - 120000).toISOString(),
    resets_at: new Date(now + 3600000).toISOString(), ...changes };
}

function planDetail(estimates) {
  return { account, snapshots: [], estimates: [], plan_estimates: [], pricing_plan_estimates: estimates };
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

test('plan page shows standard API-equivalent dollars and usage without Token or import forms', () => {
  const h = harness({ detail: { account, snapshots: [start, end], estimates: [estimate], plan_estimates: [planEstimate], pricing_plan_estimates: [pricingPlanEstimate] } });
  const tree = h.render();
  const html = renderToStaticMarkup(tree);
  const articles = nodes(tree, (node) => node.type === 'article');
  assert.equal(articles.length, 1);
  const labels = nodes(articles[0], (node) => node.type === 'dt').map((node) => node.props.children);
  assert.deepEqual(labels, [h.t.planDollarCapacity, h.t.planUsed]);
  assert.ok(html.includes('$1,234.56'));
  assert.ok(!html.includes('12.3M'));
  assert.ok(!html.includes('Token'));
  assert.ok(html.includes('12%'));
  assert.ok(html.includes(h.t.planStandardEquivalent));
  assert.ok(!html.includes(h.t.sampleCost));
  assert.equal(nodes(tree, (node) => node.type === 'textarea' || (node.type === 'input' && node.props.type === 'file')).length, 0);
  const settings = nodes(tree, (node) => node.type === 'details')[0];
  assert.ok(settings);
  assert.ok(!settings.props.open);
  assert.ok(html.includes(h.t.accountSettings));
});

test('dollar capacity requires explicit standard pricing and every required price field', () => {
  // Malformed responses exercise fail-closed rendering, not accepted API fixtures.
  for (const field of ['estimated_total_usd', 'delta_usd', 'pricing_mode', 'catalog_version', 'catalog_sha256', 'start_snapshot_id', 'interval_start']) {
    for (const value of [null, undefined]) {
      const item = { ...pricingPlanEstimate, [field]: value };
      const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [item] } });
      const article = nodes(h.render(), (node) => node.type === 'article')[0];
      const values = nodes(article, (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
      assert.ok(values[0].includes(h.t.planUnavailable), field);
      assert.ok(!values[0].includes('$'));
      assert.ok(values[1].includes('12%'));
    }
  }
});

test('Token-only responses retain window percentages without conversion to dollar capacity', () => {
  const h = harness({ detail: { account, snapshots: [], estimates: [], plan_estimates: [planEstimate] } });
  const article = nodes(h.render(), (node) => node.type === 'article')[0];
  const values = nodes(article, (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
  assert.ok(values[0].includes(h.t.planUnavailable));
  assert.ok(!values[0].includes('12.3M'));
  assert.ok(!values[0].includes('Token'));
  assert.ok(!values[0].includes('$'));
  assert.ok(values[1].includes('12%'));
});

test('one percentage point is sufficient for a complete dollar quote but zero is not', () => {
  const h = harness();
  assert.ok(h.html().includes('$1,234.56'));
  for (const changes of [{ delta_used_percent: 0 }, { delta_usd: '0' }, { estimated_total_usd: '0' }, { catalog_sha256: 'invalid' }]) {
    h.state.detail.pricing_plan_estimates = [{ ...pricingPlanEstimate, ...changes }];
    const values = nodes(nodes(h.render(), (node) => node.type === 'article')[0], (node) => node.type === 'dd')
      .map((node) => renderToStaticMarkup(node));
    assert.ok(values[0].includes(h.t.planUnavailable));
    assert.ok(!values[0].includes('$'));
  }
});

test('dollar formatting preserves decimal precision and never rounds a positive sub-cent value to zero', () => {
  for (const [value, expected] of [
    ['999.999', '$1,000.00'], ['1E-7', '&lt; $0.01'], ['9007199254740991.995', '$9,007,199,254,740,992.00'],
  ]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{
      ...pricingPlanEstimate, estimated_total_usd: value,
    }] } });
    const article = nodes(h.render(), (node) => node.type === 'article')[0];
    assert.ok(renderToStaticMarkup(article).includes(expected));
  }
});

test('missing or incomplete model pricing keeps actual usage without an invented price', () => {
  for (const reason of ['pricing_unavailable', 'pricing_incomplete']) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{
      ...pricingPlanEstimate, status: 'unavailable', reason_code: reason,
      estimated_total_usd: null, delta_usd: null, used_percent: 25,
      delta_used_percent: null, start_snapshot_id: null, interval_start: null,
    }] } });
    const values = nodes(nodes(h.render(), (node) => node.type === 'article')[0], (node) => node.type === 'dd')
      .map((node) => renderToStaticMarkup(node));
    assert.ok(values[0].includes(h.t.planUnavailable));
    assert.ok(!values[0].includes('$'));
    assert.ok(values[1].includes('25%'));
  }
});

test('cycle-anchor predictions display server totals through incomplete pricing with the latest percentage', () => {
  const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{
    ...pricingPlanEstimate, prediction_basis: 'cycle_anchor_missing_zero',
    reason_code: 'pricing_incomplete', used_percent: 25, delta_used_percent: 15,
    estimated_total_usd: '82.3040',
    last_estimated_total_usd: '111.00', last_estimate_observed_at: '2026-09-14T10:55:00Z',
  }] } });
  const article = nodes(h.render(), (node) => node.type === 'article')[0];
  const html = renderToStaticMarkup(article);
  assert.ok(html.includes('$82.30'));
  assert.ok(!html.includes('$111.00'));
  assert.ok(html.includes('25%'));
  assert.ok(html.includes(h.t.planStandardEquivalent));
  assert.ok(!html.includes('缺失'));
  assert.ok(!html.includes('沿用'));
  assert.equal(nodes(article, (node) => node.props.role === 'tooltip' || node.props.title).length, 0);
  h.state.detail.pricing_plan_estimates = [{ ...h.state.detail.pricing_plan_estimates[0],
    used_percent: 26, delta_used_percent: 16, delta_usd: '20', estimated_total_usd: '125',
  }];
  assert.ok(h.html().includes('$125.00'));
  assert.ok(h.html().includes('26%'));
  assert.ok(!h.html().includes('$82.30'));
});

test('explicit cycle-anchor predictions accept zero contributions and entirely absent price metadata', () => {
  for (const metadata of [
    {}, { pricing_mode: null, catalog_version: null, catalog_sha256: null },
  ]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{
      ...pricingPlanEstimate, ...metadata, prediction_basis: 'cycle_anchor_missing_zero',
      reason_code: 'pricing_unavailable', estimated_total_usd: '0', delta_usd: '0.0000', used_percent: 25,
    }] } });
    const values = nodes(nodes(h.render(), (node) => node.type === 'article')[0], (node) => node.type === 'dd')
      .map((node) => renderToStaticMarkup(node));
    assert.ok(values[0].includes('$0.00'));
    assert.ok(values[0].includes(h.t.planStandardEquivalent));
    assert.ok(values[1].includes('25%'));
  }
});

test('unmarked partial responses and incomplete metadata cannot opt into cycle-anchor predictions', () => {
  const partial = { ...pricingPlanEstimate, prediction_basis: 'cycle_anchor_missing_zero',
    reason_code: 'pricing_incomplete', estimated_total_usd: '0', delta_usd: '0',
    pricing_mode: null, catalog_version: null, catalog_sha256: null };
  // These deliberately malformed responses must not pass the rendering contract.
  for (const changes of [
    { prediction_basis: undefined }, { prediction_basis: null }, { prediction_basis: 'unknown' },
    { pricing_mode: 'standard_equivalent' }, { catalog_version: 'test-v1' }, { catalog_sha256: 'b'.repeat(64) },
    { pricing_mode: undefined, catalog_version: undefined, catalog_sha256: undefined },
    { reason_code: 'bucket_activity_unattributed' },
  ]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{ ...partial, ...changes }] } });
    const html = renderToStaticMarkup(nodes(h.render(), (node) => node.type === 'article')[0]);
    assert.ok(!html.includes('$'), JSON.stringify(changes));
    assert.ok(html.includes('12%'));
  }
});

test('cycle-anchor predictions never derive an absent total from a partial quote or treat it as actual zero', () => {
  for (const missing of [null, undefined]) {
    const h = harness({ detail: { account, snapshots: [start, end], estimates: [estimate], pricing_plan_estimates: [{
      ...pricingPlanEstimate, prediction_basis: 'cycle_anchor_missing_zero',
      reason_code: 'pricing_incomplete', estimated_total_usd: missing, used_percent: 25,
    }] } });
    const html = renderToStaticMarkup(nodes(h.render(), (node) => node.type === 'article')[0]);
    assert.ok(!html.includes('$'));
    assert.ok(html.includes('25%'));
  }
});

test('cycle-anchor predictions still require valid decimal amounts and a positive percentage interval', () => {
  for (const changes of [
    { estimated_total_usd: -1 }, { estimated_total_usd: '-1' }, { estimated_total_usd: 'NaN' },
    { delta_usd: null }, { delta_usd: '-1' }, { delta_used_percent: 0 }, { delta_used_percent: null },
    { used_percent: null }, { start_snapshot_id: null }, { interval_start: null },
  ]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{
      ...pricingPlanEstimate, prediction_basis: 'cycle_anchor_missing_zero', ...changes,
    }] } });
    const values = nodes(nodes(h.render(), (node) => node.type === 'article')[0], (node) => node.type === 'dd')
      .map((node) => renderToStaticMarkup(node));
    assert.ok(!values[0].includes('$'), JSON.stringify(changes));
  }
});

test('zero cycle-anchor fallback remains available while the percentage interval is collecting', () => {
  const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{
    ...pricingPlanEstimate, prediction_basis: 'cycle_anchor_missing_zero', status: 'collecting',
    reason_code: 'pricing_unavailable', estimated_total_usd: null, delta_usd: null, delta_used_percent: null,
    start_snapshot_id: null, interval_start: null, pricing_mode: null, catalog_version: null, catalog_sha256: null,
    last_estimated_total_usd: '0.00', last_estimate_observed_at: '2026-09-14T10:55:00Z', used_percent: 25,
  }] } });
  const html = renderToStaticMarkup(nodes(h.render(), (node) => node.type === 'article')[0]);
  assert.ok(html.includes('$0.00'));
  assert.ok(html.includes('25%'));
  assert.ok(!html.includes(h.t.planCollecting));
});

test('cycle-anchor estimates cannot assign main-bucket dollars to Spark or another account', () => {
  const cycle = { ...pricingPlanEstimate, prediction_basis: 'cycle_anchor_missing_zero',
    reason_code: 'pricing_incomplete', estimated_total_usd: '333.00' };
  const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [
    { ...cycle, limit_id: 'codex_bengalfox', used_percent: 17 },
    { ...cycle, limit_id: 'codex_bengalfox', window_duration_ms: 18000000, used_percent: 0 },
    { ...cycle, account_key: 'b'.repeat(64) },
  ] } });
  const articles = nodes(h.render(), (node) => node.type === 'article');
  assert.equal(articles.length, 2);
  assert.ok(!h.html().includes('$333.00'));
  assert.ok(renderToStaticMarkup(articles[0]).includes('17%'));
  assert.ok(renderToStaticMarkup(articles[1]).includes('0%'));
});

test('dollar view preserves both Spark windows, unknown names and raw quota keys', () => {
  const spark = { ...pricingPlanEstimate, limit_id: 'codex_bengalfox', status: 'unavailable',
    reason_code: 'bucket_activity_unattributed', estimated_total_usd: null, delta_usd: null,
    delta_used_percent: null, start_snapshot_id: null, interval_start: null, used_percent: 17 };
  const short = { ...spark, window_duration_ms: 18000000, used_percent: 0 };
  const unknown = { ...planEstimate, limit_id: 'constructor' };
  const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [pricingPlanEstimate, short, spark], plan_estimates: [planEstimate, unknown] } });
  const articles = nodes(h.render(), (node) => node.type === 'article');
  assert.equal(articles.length, 4);
  assert.equal(articles[0].props['aria-label'], 'Codex ' + h.t.planWeek);
  assert.equal(articles[1].props['aria-label'], 'GPT-5.3-Codex-Spark ' + h.t.planHours(5));
  assert.equal(articles[2].props['aria-label'], 'GPT-5.3-Codex-Spark ' + h.t.planWeek);
  assert.ok(articles[1].key.includes('codex_bengalfox-18000000'));
  assert.notEqual(articles[1].key, articles[2].key);
  assert.ok(renderToStaticMarkup(articles[1]).includes('0%'));
  assert.ok(renderToStaticMarkup(articles[2]).includes('17%'));
  assert.equal(articles[3].props['aria-label'], 'constructor ' + h.t.planWeek);
});

test('dollar query keeps the monitor-refreshed plan cache and renders updated server amounts', async () => {
  const h = harness();
  h.load('api/pricingPlanUsage.ts').usePricingPlanCapacity(KEY);
  const query = h.queries.at(-1);
  assert.deepEqual(query.queryKey, ['account-usage', KEY, 'plan']);
  await query.queryFn();
  assert.equal(h.calls.at(-1).path, '/api/account-usage/' + KEY + '?include_pricing=false');
  assert.ok(h.html().includes('$1,234.56'));
  h.state.detail.pricing_plan_estimates = [{ ...pricingPlanEstimate, estimated_total_usd: '9876.54' }];
  assert.ok(h.html().includes('$9,876.54'));
  assert.ok(!h.html().includes('$1,234.56'));
});

test('reset buttons target only their current Codex window and never target Spark', async () => {
  const weekly = currentPricingPlan();
  const short = { ...weekly, window_duration_ms: 18000000 };
  const spark = { ...weekly, limit_id: 'codex_bengalfox' };
  const h = harness({ detail: planDetail([weekly, short, spark]) });
  const articles = nodes(h.render(), (node) => node.type === 'article');
  assert.equal(nodes(articles[0], (node) => node.type === 'button').length, 1);
  assert.equal(nodes(articles[1], (node) => node.type === 'button').length, 1);
  assert.equal(nodes(articles[2], (node) => node.type === 'button').length, 0);
  for (const [index, duration] of [[0, 604800000], [1, 18000000]]) {
    const button = nodes(articles[index], (node) => node.type === 'button')[0];
    assert.equal(button.props.disabled, false);
    assert.equal(button.props.children, h.t.planAnchorReset);
    await button.props.onClick();
    assert.deepEqual(h.calls.at(-1), { path: `/api/account-usage/${KEY}/plan-anchor/reset`,
      options: { method: 'POST', body: JSON.stringify({ limit_id: 'codex', window_duration_ms: duration }) } });
  }
  assert.equal(h.calls.length, 2);
  assert.ok(h.calls.every((call) => call.path.endsWith('/plan-anchor/reset')));
  assert.ok(!h.html().includes('dialog'));
});

test('reset hides for invalid, expired, foreign and legacy-only plan windows', () => {
  const now = Date.now();
  const valid = currentPricingPlan();
  for (const changes of [
    { status: 'expired' }, { limit_id: 'codex_bengalfox' }, { source: 'other' },
    { account_key: 'z'.repeat(64) }, { used_percent: null }, { used_percent: -1 },
    { used_percent: 101 }, { used_percent: 1.5 }, { end_snapshot_id: '' },
    { window_duration_ms: 0 }, { window_duration_ms: -1 }, { window_duration_ms: 1.5 },
    { resets_at: 'invalid' }, { observed_at: 'invalid' },
    { resets_at: new Date(now - 60000).toISOString() },
    { observed_at: new Date(now + 60000).toISOString() },
    { observed_at: new Date(Date.parse(valid.resets_at) - valid.window_duration_ms - 1).toISOString() },
  ]) {
    const h = harness({ detail: planDetail([{ ...valid, ...changes }]) });
    assert.equal(nodes(h.render(), (node) => node.type === 'article')
      .flatMap((article) => nodes(article, (node) => node.type === 'button')).length, 0, JSON.stringify(changes));
    assert.equal(h.calls.length, 0);
  }
  const legacy = harness({ detail: { ...planDetail([]), plan_estimates: [{ ...planEstimate,
    observed_at: valid.observed_at, resets_at: valid.resets_at }] } });
  assert.equal(nodes(legacy.render(), (node) => node.type === 'article')
    .flatMap((article) => nodes(article, (node) => node.type === 'button')).length, 0);
});

test('reset permits valid collecting and incomplete windows without requiring an existing amount', () => {
  for (const changes of [
    { status: 'collecting', estimated_total_usd: null, used_percent: 0 },
    { status: 'unavailable', reason_code: 'pricing_incomplete', estimated_total_usd: null },
    { status: 'unavailable', reason_code: 'pricing_unavailable', estimated_total_usd: null },
  ]) {
    const h = harness({ detail: planDetail([currentPricingPlan(changes)]) });
    const article = nodes(h.render(), (node) => node.type === 'article')[0];
    const button = nodes(article, (node) => node.type === 'button')[0];
    assert.equal(button.props.disabled, false);
  }
});

test('reset prevents duplicate clicks and stays pending through successful plan refresh', async () => {
  let finishReset;
  let finishInvalidation;
  const original = currentPricingPlan();
  const afterReset = { ...original, status: 'collecting', estimated_total_usd: null, delta_usd: null,
    delta_used_percent: null, start_snapshot_id: null, interval_start: null,
    last_estimated_total_usd: null, last_estimate_observed_at: null };
  const h = harness({ detail: planDetail([original]), resetDetail: planDetail([afterReset]),
    resetWait: new Promise((resolve) => { finishReset = resolve; }),
    invalidationWait: new Promise((resolve) => { finishInvalidation = resolve; }) });
  const article = nodes(h.render(), (node) => node.type === 'article')[0];
  const button = nodes(article, (node) => node.type === 'button')[0];
  const first = button.props.onClick();
  await button.props.onClick();
  assert.equal(h.calls.length, 1);
  let pending = nodes(h.render(), (node) => node.type === 'button' && node.props.children === h.t.planAnchorResetting)[0];
  assert.equal(pending.props.disabled, true);
  assert.equal(pending.props['aria-busy'], true);
  assert.ok(h.html().includes('$1,234.56'));
  finishReset();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(h.invalidations, [{ queryKey: ['account-usage', KEY, 'plan'], exact: true }]);
  pending = nodes(h.render(), (node) => node.type === 'button' && node.props.children === h.t.planAnchorResetting)[0];
  assert.equal(pending.props.disabled, true);
  await pending.props.onClick();
  assert.equal(h.calls.length, 1);
  finishInvalidation();
  await first;
  assert.ok(!h.html().includes('$1,234.56'));
  assert.ok(h.html().includes(h.t.planCollecting));
  assert.equal(nodes(h.render(), (node) => node.type === 'button' && node.props.children === h.t.planAnchorReset)[0].props.disabled, false);
  h.state.detail = planDetail([{ ...original, estimated_total_usd: '50.00' }]);
  assert.ok(h.html().includes('$50.00'));
  assert.equal(h.calls.length, 1);
});

test('failed reset preserves the old amount, shows the error and allows retry', async () => {
  const h = harness({ detail: planDetail([currentPricingPlan()]), resetError: 'No current saved window' });
  const article = nodes(h.render(), (node) => node.type === 'article')[0];
  await nodes(article, (node) => node.type === 'button')[0].props.onClick();
  assert.ok(h.html().includes('$1,234.56'));
  assert.ok(nodes(h.render(), (node) => node.props?.role === 'alert')
    .some((node) => node.props.children === 'No current saved window'));
  assert.equal(h.invalidations.length, 0);
  const button = nodes(h.render(), (node) => node.type === 'button' && node.props.children === h.t.planAnchorReset)[0];
  assert.equal(button.props.disabled, false);
  delete h.state.resetError;
  await button.props.onClick();
  assert.ok(!h.html().includes('No current saved window'));
  assert.deepEqual(h.invalidations, [{ queryKey: ['account-usage', KEY, 'plan'], exact: true }]);
  assert.equal(h.calls.length, 2);
  assert.ok(h.calls.every((call) => call.path.endsWith('/plan-anchor/reset')));
});

test('reset mutation encodes account paths, strips unsupported request fields and does not retry writes', async () => {
  const anchor = { account_key: KEY, limit_id: 'codex', window_duration_ms: 18000000,
    snapshot_id: 'latest-server-selected', observed_at: end.observed_at, used_percent: 12,
    resets_at: start.resets_at, reset_at: end.observed_at, revision: 1 };
  const h = harness({ resetResult: anchor });
  h.load('api/pricingPlanUsage.ts').useResetPricingPlanAnchor('account/one');
  const mutation = h.mutations.at(-1);
  const result = await mutation.mutationFn({ limit_id: 'codex', window_duration_ms: 18000000,
    snapshot_id: 'client-cannot-select', resets_at: 'client-cannot-select' });
  assert.deepEqual(result, anchor);
  assert.equal(mutation.retry, false);
  assert.deepEqual(h.calls, [{ path: '/api/account-usage/account%2Fone/plan-anchor/reset',
    options: { method: 'POST', body: '{"limit_id":"codex","window_duration_ms":18000000}' } }]);
  await mutation.onSuccess(result);
  assert.deepEqual(h.invalidations, [{ queryKey: ['account-usage', 'account/one', 'plan'], exact: true }]);
});

test('incomplete pricing retains a valid previous amount with the latest percentage and no extra provenance label', () => {
  const lastAt = '2026-09-14T10:55:00Z';
  const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{
    ...pricingPlanEstimate, status: 'unavailable', reason_code: 'pricing_incomplete',
    estimated_total_usd: null, delta_usd: null, delta_used_percent: null,
    start_snapshot_id: null, interval_start: null, used_percent: 25,
    pricing_mode: null, catalog_version: null, catalog_sha256: null,
    last_estimated_total_usd: '111.00', last_estimate_observed_at: lastAt,
  }] } });
  const article = nodes(h.render(), (node) => node.type === 'article')[0];
  const values = nodes(article, (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
  assert.equal(values.length, 2);
  assert.ok(values[0].includes('$111.00'));
  assert.ok(values[0].includes(h.t.planStandardEquivalent));
  assert.ok(!values[0].includes(h.t.planUnavailable));
  assert.ok(values[1].includes('25%'));
  const html = renderToStaticMarkup(article);
  assert.ok(!html.includes(lastAt));
  assert.ok(!html.includes('沿用上次'));
  assert.ok(!html.includes('本轮数据待补齐'));
});

test('a current valid dollar estimate takes priority over an older retained estimate', () => {
  const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{
    ...pricingPlanEstimate, last_estimated_total_usd: '111.00', last_estimate_observed_at: '2026-09-14T10:55:00Z',
  }] } });
  const html = renderToStaticMarkup(nodes(h.render(), (node) => node.type === 'article')[0]);
  assert.ok(html.includes('$1,234.56'));
  assert.ok(!html.includes('$111.00'));
  assert.ok(html.includes('12%'));
});

test('retained estimates require a positive decimal string paired with a valid past timestamp', () => {
  const prior = { ...pricingPlanEstimate, status: 'unavailable', reason_code: 'pricing_incomplete',
    estimated_total_usd: null, delta_usd: null, delta_used_percent: null,
    start_snapshot_id: null, interval_start: null,
    last_estimated_total_usd: '111.00', last_estimate_observed_at: '2026-09-14T10:55:00Z' };
  for (const changes of [
    { last_estimated_total_usd: null }, { last_estimated_total_usd: undefined },
    { last_estimated_total_usd: 111 }, { last_estimated_total_usd: '0' }, { last_estimated_total_usd: 'invalid' },
    { last_estimate_observed_at: null }, { last_estimate_observed_at: undefined },
    { last_estimate_observed_at: 1789383300000 }, { last_estimate_observed_at: 'invalid' },
    { last_estimate_observed_at: '2026-09-14T10:55:00' },
    { last_estimate_observed_at: '2026-09-14T11:01:00Z' },
  ]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{ ...prior, ...changes }] } });
    const values = nodes(nodes(h.render(), (node) => node.type === 'article')[0], (node) => node.type === 'dd')
      .map((node) => renderToStaticMarkup(node));
    assert.ok(values[0].includes(h.t.planUnavailable));
    assert.ok(!values[0].includes('$'));
    assert.ok(values[1].includes('12%'));
  }
});

test('previous estimates never bleed into Spark buckets or expired windows', () => {
  const previous = { ...pricingPlanEstimate, status: 'unavailable', reason_code: 'pricing_incomplete',
    estimated_total_usd: null, delta_usd: null, delta_used_percent: null,
    start_snapshot_id: null, interval_start: null,
    last_estimated_total_usd: '111.00', last_estimate_observed_at: '2026-09-14T10:55:00Z' };
  for (const changes of [
    { limit_id: 'codex_bengalfox', reason_code: 'bucket_activity_unattributed' },
    { status: 'expired', reason_code: null, used_percent: null },
  ]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{ ...previous, ...changes }] } });
    const article = nodes(h.render(), (node) => node.type === 'article')[0];
    const html = renderToStaticMarkup(article);
    assert.ok(!html.includes('$111.00'));
    if (changes.limit_id) {
      assert.equal(article.props['aria-label'], 'GPT-5.3-Codex-Spark ' + h.t.planWeek);
      assert.ok(html.includes('12%'));
    }
  }
});

test('old API responses with missing or null previous estimate fields keep existing behavior', () => {
  for (const changes of [{}, { last_estimated_total_usd: null, last_estimate_observed_at: null }]) {
    const h = harness({ detail: { account, snapshots: [], estimates: [], pricing_plan_estimates: [{ ...pricingPlanEstimate, ...changes }] } });
    assert.ok(h.html().includes('$1,234.56'));
    h.state.detail.pricing_plan_estimates = [{ ...pricingPlanEstimate, ...changes, status: 'unavailable',
      reason_code: 'pricing_incomplete', estimated_total_usd: null, delta_usd: null,
      delta_used_percent: null, start_snapshot_id: null, interval_start: null }];
    const values = nodes(nodes(h.render(), (node) => node.type === 'article')[0], (node) => node.type === 'dd')
      .map((node) => renderToStaticMarkup(node));
    assert.ok(values[0].includes(h.t.planUnavailable));
    assert.ok(!values[0].includes('$'));
  }
});

test('plan status placeholders preserve unknown versus zero and hide expired percentages', () => {
  for (const [status, text, used] of [
    ['collecting', 'planCollecting', 25], ['unavailable', 'planUnavailable', null], ['expired', 'planExpired', 25],
  ]) {
    const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
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
  const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
    ...planEstimate, status: 'unavailable', used_percent: 0, estimated_total_tokens: null,
    delta_tokens: null, delta_used_percent: null, start_snapshot_id: null, interval_start: null,
  }] } });
  assert.ok(h.html().includes('0%'));
});

test('missing aligned activity is visible in both languages while the real percentage remains', () => {
  for (const [lang, expected] of [['zh', '缺少同步用量数据'], ['en', 'Aligned usage unavailable']]) {
    const h = harness({ tokenView: true, lang, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
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
  const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
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
    const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
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
    const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [item] } });
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
      const h = harness({ tokenView: true, lang, detail: { account, snapshots: [], estimates: [], plan_estimates: [item] } });
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
    const h = harness({ tokenView: true, lang, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
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
    const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
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
    const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
      ...planEstimate, estimated_total_tokens: value,
    }] } });
    const html = renderToStaticMarkup(nodes(h.render(), (node) => node.type === 'article')[0]);
    assert.ok(html.includes(expected), html);
    assert.ok(html.includes('native_activity'));
  }
  const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [{
    ...planEstimate, estimated_total_tokens: Number.MAX_SAFE_INTEGER + 1,
  }] } });
  const html = renderToStaticMarkup(nodes(h.render(), (node) => node.type === 'article')[0]);
  assert.ok(html.includes(h.t.planUnavailable));
});

test('every plan bucket has its own window and the page reads the latest capacity result', () => {
  const second = { ...planEstimate, limit_id: 'short-bucket', window_duration_ms: 18000000, estimated_total_tokens: 2500000000 };
  const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [planEstimate, second] } });
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

test('known plan IDs render formal model names in visible labels and accessibility names', () => {
  for (const lang of ['zh', 'en']) {
    const spark = {
      ...planEstimate, limit_id: 'codex_bengalfox', status: 'unavailable',
      reason_code: 'bucket_activity_unattributed', used_percent: 0,
      estimated_total_tokens: null, delta_tokens: null, delta_used_percent: null,
      start_snapshot_id: null, interval_start: null,
    };
    const h = harness({ tokenView: true, lang, detail: { account, snapshots: [], estimates: [], plan_estimates: [planEstimate, spark] } });
    const articles = nodes(h.render(), (node) => node.type === 'article');
    assert.equal(articles.length, 2);
    for (const [index, name] of [[0, 'Codex'], [1, 'GPT-5.3-Codex-Spark']]) {
      const header = nodes(articles[index], (node) => node.type === 'header')[0];
      const nameNode = nodes(header, (node) => node.type === 'span')[0];
      const visibleName = React.Children.toArray(nameNode.props.children)
        .filter((child) => typeof child === 'string').join('').trim();
      assert.equal(visibleName, name);
      assert.equal(articles[index].props['aria-label'], name + ' ' + h.t.planWeek);
      assert.ok(!renderToStaticMarkup(header).includes('codex_bengalfox'));
      assert.ok(!articles[index].props['aria-label'].includes('codex_bengalfox'));
    }
    const sparkValues = nodes(articles[1], (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
    assert.ok(sparkValues[0].includes(h.t.planUnavailable));
    assert.ok(sparkValues[1].includes('0%'));
  }
});

test('unknown plan IDs remain verbatim, including object prototype property names', () => {
  const ids = ['unlisted-model', 'constructor'];
  const planEstimates = ids.map((id) => ({
    ...planEstimate, limit_id: id, status: 'unavailable', reason_code: 'bucket_activity_unattributed',
    estimated_total_tokens: null, delta_tokens: null, delta_used_percent: null,
    start_snapshot_id: null, interval_start: null,
  }));
  const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: planEstimates } });
  const articles = nodes(h.render(), (node) => node.type === 'article');
  assert.equal(articles.length, ids.length);
  for (const [index, id] of ids.entries()) {
    const header = nodes(articles[index], (node) => node.type === 'header')[0];
    const nameNode = nodes(header, (node) => node.type === 'span')[0];
    const visibleName = React.Children.toArray(nameNode.props.children)
      .filter((child) => typeof child === 'string').join('').trim();
    assert.equal(visibleName, id);
    assert.equal(articles[index].props['aria-label'], id + ' ' + h.t.planWeek);
  }
});

test('renamed Codex five-hour and weekly cards stay separate with unchanged values and raw-ID keys', () => {
  const short = { ...planEstimate, window_duration_ms: 18000000, resets_at: '2026-09-14T15:00:00Z',
    estimated_total_tokens: 2500000, delta_tokens: 50000, used_percent: 25 };
  const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [short, planEstimate] } });
  const articles = nodes(h.render(), (node) => node.type === 'article');
  assert.equal(articles.length, 2);
  assert.notEqual(articles[0].key, articles[1].key);
  for (const [index, item, window, capacity, used] of [
    [0, short, h.t.planHours(5), '2.5M', '25%'],
    [1, planEstimate, h.t.planWeek, '12.3M', '12%'],
  ]) {
    assert.equal(articles[index].props['aria-label'], 'Codex ' + window);
    assert.ok(articles[index].key.includes(item.limit_id + '-' + item.window_duration_ms));
    const values = nodes(articles[index], (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
    assert.equal(values.length, 2);
    assert.ok(values[0].includes(capacity));
    assert.ok(values[1].includes(used));
  }
});

test('Spark five-hour and weekly unavailable cards both retain their own observed percentages', () => {
  const sparkWeek = {
    ...planEstimate, limit_id: 'codex_bengalfox', status: 'unavailable',
    reason_code: 'bucket_activity_unattributed', used_percent: 17,
    estimated_total_tokens: null, delta_tokens: null, delta_used_percent: null,
    start_snapshot_id: null, interval_start: null,
  };
  const sparkShort = { ...sparkWeek, window_duration_ms: 18000000,
    resets_at: '2026-09-14T15:00:00Z', used_percent: 0 };
  const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [sparkShort, sparkWeek] } });
  const articles = nodes(h.render(), (node) => node.type === 'article');
  assert.equal(articles.length, 2);
  assert.notEqual(articles[0].key, articles[1].key);
  for (const [index, item, window, used] of [
    [0, sparkShort, h.t.planHours(5), '0%'],
    [1, sparkWeek, h.t.planWeek, '17%'],
  ]) {
    assert.equal(articles[index].props['aria-label'], 'GPT-5.3-Codex-Spark ' + window);
    assert.ok(!articles[index].props['aria-label'].includes('codex_bengalfox'));
    assert.ok(articles[index].key.includes(item.limit_id + '-' + item.window_duration_ms));
    const values = nodes(articles[index], (node) => node.type === 'dd').map((node) => renderToStaticMarkup(node));
    assert.equal(values.length, 2);
    assert.ok(values[0].includes(h.t.planUnavailable));
    assert.ok(!values[0].includes('Token'));
    assert.ok(values[1].includes(used));
  }
});

test('plan results never mix another account into the selected account card', () => {
  const h = harness({ tokenView: true, detail: { account, snapshots: [], estimates: [], plan_estimates: [
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

test('monitor intervals only accept whole seconds from 30 through 1800', () => {
  const { monitorIntervalMilliseconds } = harness().load('lib/account-usage.ts');
  assert.equal(monitorIntervalMilliseconds('30'), 30000);
  assert.equal(monitorIntervalMilliseconds('300'), 300000);
  assert.equal(monitorIntervalMilliseconds('1800'), 1800000);
  for (const value of ['', '29', '1801', '30.5', 'NaN', '-30', '1e3']) {
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

test('an unconfigured monitor stays disabled until a verified capture or explicit enable', async () => {
  const h = harness();
  const initial = h.html();
  assert.ok(initial.includes(h.t.monitorDisabled));
  assert.ok(initial.includes(h.t.monitorRuntimeStopped));
  assert.ok(initial.includes(h.t.monitorSingleSource));
  assert.equal(h.calls.length, 0);
  const field = nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0];
  field.props.onChange({ target: { value: '900' } });
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

test('a server-enabled verified account is displayed without a redundant settings write', () => {
  const h = harness({ monitor: { ...monitorState, status: 'waiting', runtime_running: true,
    settings: { enabled: true, interval_ms: 1800000 }, revision: 1 } });
  assert.ok(h.html().includes(h.t.monitorEnabled));
  assert.ok(h.html().includes(h.t.monitorWaiting));
  assert.ok(!h.html().includes(h.t.monitorRuntimeStopped));
  assert.equal(h.calls.length, 0);
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

test('monitor displays persisted intervals in seconds without changing five-minute settings or defaults', () => {
  for (const [enabled, interval, displayed] of [[true, 300000, '300'], [false, 1800000, '1800']]) {
    const h = harness({ monitor: { ...monitorState, settings: { enabled, interval_ms: interval }, runtime_running: true } });
    const field = nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0];
    assert.equal(field.props.value, displayed);
    assert.equal(field.props.min, 30);
    assert.equal(field.props.max, 1800);
    assert.equal(field.props.step, 1);
    assert.equal(h.calls.length, 0);
    assert.deepEqual(h.state.monitor.settings, { enabled, interval_ms: interval });
  }
});

test('monitor submits the thirty-second and thirty-minute boundaries as exact milliseconds', async () => {
  for (const [seconds, interval] of [['30', 30000], ['1800', 1800000]]) {
    const h = harness({ monitor: { ...monitorState, runtime_running: true } });
    const field = nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0];
    field.props.onChange({ target: { value: seconds } });
    const form = nodes(h.render(), (node) => node.type === 'form' && node.props['aria-label'] === h.t.monitorTitle)[0];
    form.props.onSubmit({ preventDefault() {} });
    await new Promise(setImmediate);
    assert.deepEqual(h.calls, [{ monitor: { key: KEY, settings: { enabled: true, interval_ms: interval } } }]);
    assert.equal(nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0].props.value, seconds);
  }
});

test('invalid monitor seconds never issue a settings write or alter the stored interval', async () => {
  for (const value of ['29', '1801', '30.5', '']) {
    const h = harness({ monitor: { ...monitorState, runtime_running: true } });
    nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0].props.onChange({ target: { value } });
    const form = nodes(h.render(), (node) => node.type === 'form' && node.props['aria-label'] === h.t.monitorTitle)[0];
    form.props.onSubmit({ preventDefault() {} });
    await new Promise(setImmediate);
    assert.equal(h.calls.length, 0);
    assert.equal(h.state.monitor.settings.interval_ms, 300000);
    assert.ok(h.html().includes(h.t.monitorIntervalError));
  }
});

test('pause submits only disabled settings and preserves the saved interval', async () => {
  const h = harness({ monitor: { ...monitorState, settings: { enabled: true, interval_ms: 600000 },
    status: 'waiting', runtime_running: true } });
  nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0].props.onChange({ target: { value: '' } });
  const pause = nodes(h.render(), (node) => node.type === 'button' && node.props.children === h.t.monitorPause)[0];
  pause.props.onClick(); await new Promise(setImmediate);
  assert.deepEqual(h.calls.at(-1), { monitor: { key: KEY, settings: { enabled: false } } });
  assert.equal(h.state.monitor.settings.interval_ms, 600000);
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
  await mutation.mutationFn({ key: KEY, settings: { enabled: false } });
  assert.deepEqual(JSON.parse(h.calls.at(-1).options.body), { enabled: false });
});

test('legacy twenty-four-hour monitoring can pause without resubmitting or shortening its interval', async () => {
  const h = harness({ monitor: { ...monitorState, settings: { enabled: true, interval_ms: 86400000 },
    status: 'waiting', runtime_running: true } });
  const field = () => nodes(h.render(), (node) => node.type === 'input' && node.props.type === 'number')[0];
  assert.equal(field().props.value, '86400');
  assert.equal(field().props.max, 1800);
  assert.equal(h.calls.length, 0);
  const form = nodes(h.render(), (node) => node.type === 'form' && node.props['aria-label'] === h.t.monitorTitle)[0];
  const save = nodes(form, (node) => node.type === 'button' && node.props.type === 'submit')[0];
  assert.equal(save.props.disabled, true);
  const pause = nodes(form, (node) => node.type === 'button' && node.props.children === h.t.monitorPause)[0];
  assert.equal(pause.props.disabled, false);
  pause.props.onClick(); await new Promise(setImmediate);
  assert.deepEqual(h.calls, [{ monitor: { key: KEY, settings: { enabled: false } } }]);
  assert.deepEqual(h.state.monitor.settings, { enabled: false, interval_ms: 86400000 });
  assert.equal(field().props.value, '86400');
  assert.ok(h.html().includes(h.t.monitorPaused));
  field().props.onChange({ target: { value: '30' } });
  nodes(h.render(), (node) => node.type === 'form' && node.props['aria-label'] === h.t.monitorTitle)[0].props.onSubmit({ preventDefault() {} });
  await new Promise(setImmediate);
  assert.deepEqual(h.calls.at(-1), { monitor: { key: KEY, settings: { enabled: true, interval_ms: 30000 } } });
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
