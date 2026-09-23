import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const React = require('react');
const ts = require('typescript');
const { QueryClient, QueryObserver, MutationObserver } = require('@tanstack/react-query');

function compile(relative, mocks, globals = {}) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8')
    .replaceAll('import.meta.env', '({})');
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  new Function('require', 'exports', ...Object.keys(globals), code)(
    (name) => mocks[name] ?? require(name), exports, ...Object.values(globals),
  );
  return exports;
}

// Real QueryClient/observers execute reads, mutations, cancellation and cache updates.
// Only React's render boundary is simulated; no separate imitation of API validation.
function harness({ fetch, cached = 'en', storageUnavailable = false } = {}) {
  const values = new Map([['lang', cached]]);
  const localStorage = {
    getItem: (key) => { if (storageUnavailable) throw new Error('disabled'); return values.get(key); },
    setItem: (key, value) => { if (storageUnavailable) throw new Error('disabled'); values.set(key, value); },
  };
  const calls = [];
  const api = compile('../src/api/client.ts', {}, {
    fetch: (...args) => { calls.push(args); return fetch(...args); },
    window: { location: { host: 'localhost' } },
  });
  const client = new QueryClient({ defaultOptions: {
    queries: { retry: false, gcTime: Infinity }, mutations: { retry: false, gcTime: Infinity },
  } });
  let query;
  let mutation;
  let cachedLang;
  const unsubscribers = [];
  const useMutation = (options) => {
    if (!mutation) {
      mutation = new MutationObserver(client, options);
      unsubscribers.push(mutation.subscribe(() => {}));
    }
    return { ...mutation.getCurrentResult(), mutate: (mode) => { mutation.mutate(mode).catch(() => {}); } };
  };
  const module = compile('../src/i18n/index.ts', {
    react: { ...React, useState: (initial) => [cachedLang ??= initial(), () => {}],
      useCallback: (fn) => fn, useEffect: (fn) => fn() },
    '@tanstack/react-query': {
      useQueryClient: () => client,
      useQuery: (options) => {
        if (!query) {
          query = new QueryObserver(client, options);
          unsubscribers.push(query.subscribe(() => {}));
        }
        return query.getCurrentResult();
      },
      useMutation,
    },
    '@/api/client': api,
    './zh': { zh: { name: 'Chinese' } },
    './en': { en: { name: 'English' } },
  }, { localStorage, navigator: { language: 'en-US' } });
  return {
    calls, values, render: () => module.useLanguage(),
    async settled() {
      for (let i = 0; i < 100; i++) {
        await new Promise((done) => setImmediate(done));
        if (!client.isFetching() && !client.isMutating()) return;
      }
      throw new Error('Language request did not settle');
    },
    async refetch() { return query.refetch(); },
    close() { unsubscribers.forEach((unsubscribe) => unsubscribe()); client.clear(); },
  };
}

const response = (mode, effective, source = mode === 'follow' ? 'system' : 'dashboard') =>
  Response.json({ mode, effective, source });

test('startup reads authoritative effective language without promoting the old browser value', async () => {
  const h = harness({ cached: 'zh', fetch: async () => response('follow', 'en') });
  try {
    assert.equal(h.render().lang, 'zh');
    await h.settled();
    const state = h.render();
    assert.equal(state.lang, 'en');
    assert.equal(state.mode, 'follow');
    assert.equal(state.error, null);
    assert.equal(h.values.get('lang'), 'en');
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0][0], '/api/settings/language?host=system');
    assert.equal(h.calls[0][1].method, undefined);
  } finally { h.close(); }
});

test('manual selection persists via PUT and a new client reads it; follow remains selectable', async () => {
  let mode = 'follow';
  const fetch = async (_url, options) => {
    if (options.method === 'PUT') mode = JSON.parse(options.body).mode;
    return response(mode, mode === 'follow' ? 'en' : mode);
  };
  const first = harness({ fetch });
  try {
    first.render();
    await first.settled();
    first.render().switchLang('zh');
    await first.settled();
    assert.equal(first.render().lang, 'zh');
    assert.equal(first.render().mode, 'zh');
    assert.deepEqual(JSON.parse(first.calls[1][1].body), { mode: 'zh' });
  } finally { first.close(); }
  const second = harness({ fetch, cached: 'en' });
  try {
    second.render();
    await second.settled();
    assert.equal(second.render().lang, 'zh');
    assert.equal(second.render().mode, 'zh');
    second.render().switchLang('follow');
    await second.settled();
    assert.equal(second.render().mode, 'follow');
    assert.equal(second.render().lang, 'en');
  } finally { second.close(); }
});

test('failed save keeps the previous effective language, mode and cache with an error', async () => {
  const h = harness({ cached: 'zh', fetch: async (_url, options) => options.method === 'PUT'
    ? Response.json({ detail: 'read-only configuration' }, { status: 500 }) : response('zh', 'zh') });
  try {
    h.render();
    await h.settled();
    h.render().switchLang('en');
    await h.settled();
    assert.equal(h.render().lang, 'zh');
    assert.equal(h.render().mode, 'zh');
    assert.equal(h.render().error, 'save');
    assert.equal(h.values.get('lang'), 'zh');
    assert.equal(h.calls.length, 2);
  } finally { h.close(); }
});

test('failed startup uses the cache and allows a later explicit save', async () => {
  const h = harness({ cached: 'zh', fetch: async (_url, options) => {
    if (options.method !== 'PUT') throw new TypeError('offline');
    return response('en', 'en');
  } });
  try {
    h.render();
    await h.settled();
    assert.equal(h.render().lang, 'zh');
    assert.equal(h.render().error, 'load');
    h.render().switchLang('en');
    await h.settled();
    assert.equal(h.render().lang, 'en');
    assert.equal(h.render().error, null);
  } finally { h.close(); }
});

test('optional browser storage and malformed server replies do not crash the interface', async () => {
  const h = harness({ storageUnavailable: true, fetch: async (_url, options) => options.method === 'PUT'
    ? Response.json({ mode: 'en', effective: 'unknown' }) : response('zh', 'zh') });
  try {
    h.render();
    await h.settled();
    assert.equal(h.render().lang, 'zh');
    h.render().switchLang('en');
    await h.settled();
    assert.equal(h.render().lang, 'zh');
    assert.equal(h.render().error, 'save');
  } finally { h.close(); }
});

test('a stale in-flight read cannot roll back a successfully saved choice', async () => {
  let finishRead;
  let reads = 0;
  const h = harness({ fetch: async (_url, options) => {
    if (options.method === 'PUT') return response('zh', 'zh');
    reads += 1;
    return reads === 1 ? response('follow', 'en') : new Promise((done) => { finishRead = done; });
  } });
  try {
    h.render();
    await h.settled();
    const staleRead = h.refetch();
    h.render().switchLang('zh');
    await h.settled();
    finishRead(response('follow', 'en'));
    await staleRead;
    await h.settled();
    assert.equal(h.render().lang, 'zh');
    assert.equal(h.render().mode, 'zh');
    assert.equal(h.values.get('lang'), 'zh');
  } finally { h.close(); }
});
