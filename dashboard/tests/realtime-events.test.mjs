import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import ts from 'typescript';
import { QueryClient, QueryObserver } from '@tanstack/react-query';

const require = createRequire(import.meta.url);

function harness(t) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const events = [];
  const invalidations = [];
  const effects = [];
  let options;
  const original = client.invalidateQueries.bind(client);
  client.invalidateQueries = (filters, options) => {
    invalidations.push(filters.queryKey[0]);
    return original(filters, options);
  };
  const mocks = {
    react: {
      useCallback: (callback) => callback,
      useRef: (current) => ({ current }),
      useMemo: (factory) => factory(),
      useEffect: (effect) => effects.push(effect),
    },
    'react-use-websocket': { default: (_, value) => { options = value; return {}; } },
    '@tanstack/react-query': { useQueryClient: () => client },
    '../api/client': { WS_URL: 'ws://test.invalid' },
    '../stores/websocket': { useWSStore: () => ({ addEvent: (event) => events.push(event), setConnected() {} }) },
  };
  function load(path) {
    const source = readFileSync(path, 'utf8');
    const code = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const module = { exports: {} };
    new Function('require', 'module', 'exports', code)((name) => {
      if (name in mocks) return mocks[name];
      if (name.startsWith('.')) return load(new URL(`${name}.ts`, path));
      return require(name);
    }, module, module.exports);
    return module.exports;
  }
  load(new URL('../src/hooks/useRealtimeEvents.ts', import.meta.url)).useRealtimeEvents();
  let cleanups = effects.map((effect) => effect());
  const unmount = () => cleanups.forEach((cleanup) => cleanup?.());
  t.after(() => { unmount(); client.clear(); });
  return {
    client, events, invalidations,
    emit(type, data = {}) { options.onMessage({ data: JSON.stringify({ type: 'event', event_type: type, data }) }); },
    raw(data) { options.onMessage({ data }); },
    unmount,
    remountEffects() { unmount(); cleanups = effects.map((effect) => effect()); },
  };
}

async function flushMicrotasks() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
}

function slowObserver(t, h, initial = true) {
  let version = 0;
  let aborted = 0;
  const requests = [];
  const observer = new QueryObserver(h.client, {
    queryKey: ['teams', 'team-id', 'agents'],
    ...(initial ? { initialData: 0 } : {}),
    queryFn: ({ signal }) => new Promise((resolve, reject) => {
      const snapshot = version;
      requests.push({ resolve: () => resolve(snapshot), reject });
      signal.addEventListener('abort', () => { aborted++; reject(new Error('aborted')); });
    }),
  });
  const unsubscribe = observer.subscribe(() => {});
  t.after(unsubscribe);
  return { requests, setVersion(value) { version = value; }, get aborted() { return aborted; } };
}

test('slow query is not cancelled by continuous events and final dirty state is fetched', async (t) => {
  const h = harness(t);
  const slow = slowObserver(t, h);
  for (let i = 1; i <= 10; i++) {
    slow.setVersion(i);
    h.emit('cc.tool_use');
    t.mock.timers.tick(100);
    await flushMicrotasks();
  }
  assert.equal(slow.aborted, 0);
  assert.equal(slow.requests.length, 1);
  slow.requests[0].resolve();
  await flushMicrotasks();
  t.mock.timers.tick(200);
  assert.equal(slow.requests.length, 2);
  slow.requests[1].resolve();
  await flushMicrotasks();
  assert.equal(h.client.getQueryData(['teams', 'team-id', 'agents']), 10);
  t.mock.timers.tick(10000);
  await flushMicrotasks();
  assert.equal(slow.requests.length, 2);
});

test('event during initial query gets one fresh follow-up without cancellation', async (t) => {
  const h = harness(t);
  const slow = slowObserver(t, h, false);
  slow.setVersion(1);
  h.emit('agent.updated');
  t.mock.timers.tick(200);
  assert.equal(slow.requests.length, 1);
  slow.requests[0].resolve();
  await flushMicrotasks();
  t.mock.timers.tick(200);
  assert.equal(slow.requests.length, 2);
  slow.requests[1].resolve();
  await flushMicrotasks();
  assert.equal(h.client.getQueryData(['teams', 'team-id', 'agents']), 1);
  assert.equal(slow.aborted, 0);
});

test('unmount while dirty fetch is pending cannot schedule a follow-up', async (t) => {
  const h = harness(t);
  const slow = slowObserver(t, h);
  h.emit('agent.updated');
  t.mock.timers.tick(200);
  h.emit('agent.updated');
  h.unmount();
  slow.requests[0].resolve();
  await flushMicrotasks();
  t.mock.timers.tick(10000);
  assert.equal(slow.requests.length, 1);
});

test('failed dirty fetch gets one event-driven follow-up, not an endless retry', async (t) => {
  const h = harness(t);
  const slow = slowObserver(t, h);
  h.emit('agent.updated');
  t.mock.timers.tick(200);
  h.emit('agent.updated');
  t.mock.timers.tick(200);
  slow.requests[0].reject(new Error('offline'));
  await flushMicrotasks();
  t.mock.timers.tick(200);
  assert.equal(slow.requests.length, 2);
  slow.requests[1].reject(new Error('still offline'));
  await flushMicrotasks();
  t.mock.timers.tick(10000);
  assert.equal(slow.requests.length, 2);
});

test('inactive background fetch does not create an event refresh loop', async (t) => {
  const h = harness(t);
  let finish;
  const fetch = h.client.fetchQuery({
    queryKey: ['teams', 'inactive'],
    queryFn: () => new Promise((resolve) => { finish = resolve; }),
  });
  h.emit('team.updated');
  t.mock.timers.tick(200);
  await flushMicrotasks();
  t.mock.timers.tick(10000);
  await flushMicrotasks();
  assert.equal(h.invalidations.filter((key) => key === 'teams').length, 1);
  finish([]);
  await fetch;
});

test('Strict Mode remount isolates pending request settlement from the old effect', async (t) => {
  const h = harness(t);
  const slow = slowObserver(t, h);
  h.emit('agent.updated');
  t.mock.timers.tick(200);
  h.emit('agent.updated');
  h.remountEffects();
  slow.setVersion(2);
  h.emit('agent.updated');
  t.mock.timers.tick(200);
  assert.equal(slow.requests.length, 1);
  slow.requests[0].resolve();
  await flushMicrotasks();
  t.mock.timers.tick(200);
  assert.equal(slow.requests.length, 2);
  slow.requests[1].resolve();
  await flushMicrotasks();
  t.mock.timers.tick(10000);
  assert.equal(slow.requests.length, 2);
  assert.equal(h.client.getQueryData(['teams', 'team-id', 'agents']), 2);
});

test('cc burst preserves every event but refetches active prefix queries once', async (t) => {
  const h = harness(t);
  let requests = 0;
  const observer = new QueryObserver(h.client, {
    queryKey: ['teams', 'team-id', 'agents'], initialData: [],
    queryFn: async () => { requests++; return []; },
  });
  const unsubscribe = observer.subscribe(() => {});
  t.after(unsubscribe);
  for (let i = 0; i < 20; i++) {
    h.emit('cc.tool_use', { sequence: i });
    await Promise.resolve();
    await Promise.resolve();
  }
  assert.equal(h.events.length, 20);
  assert.deepEqual(h.events.map((event) => event.data.sequence), Array.from({ length: 20 }, (_, i) => i));
  assert.equal(h.invalidations.length, 0);
  t.mock.timers.tick(200);
  await Promise.resolve();
  assert.deepEqual(h.invalidations.sort(), ['activities', 'events', 'teams']);
  assert.equal(requests, 1);
});

test('overlapping cc, agent and task events deduplicate without project metadata', (t) => {
  const h = harness(t);
  ['cc.tool_use', 'agent.updated', 'task.updated'].forEach((type) => h.emit(type));
  t.mock.timers.tick(200);
  assert.deepEqual(h.invalidations.sort(), ['activities', 'events', 'project-task-wall', 'task-wall', 'tasks', 'teams']);
});

test('continuous events cannot postpone the first deadline or subsequent windows', async (t) => {
  const h = harness(t);
  for (let i = 0; i < 10; i++) {
    h.emit('cc.tool_use');
    t.mock.timers.tick(50);
    await flushMicrotasks();
  }
  assert.equal(h.events.length, 10);
  assert.equal(h.invalidations.filter((key) => key === 'teams').length, 2);
  t.mock.timers.tick(100);
  assert.equal(h.invalidations.filter((key) => key === 'teams').length, 3);
});

test('unmount cancels pending refresh and ignores late callbacks', (t) => {
  const h = harness(t);
  h.emit('cc.tool_use');
  h.unmount();
  h.emit('cc.tool_complete');
  t.mock.timers.tick(1000);
  assert.deepEqual(h.invalidations, []);
});

test('Strict Mode effect cleanup and setup permits fresh events', (t) => {
  const h = harness(t);
  h.emit('team.updated');
  h.remountEffects();
  h.emit('task.updated');
  t.mock.timers.tick(200);
  assert.deepEqual(h.invalidations.sort(), ['events', 'project-task-wall', 'task-wall', 'tasks', 'teams']);
});

test('all existing event prefix matches remain unchanged', (t) => {
  const h = harness(t);
  ['team.updated', 'task.updated', 'agent.updated', 'meeting.updated', 'workflow.started', 'project.updated', 'cc.tool_use', 'other'].forEach((type) => h.emit(type));
  h.raw('{bad');
  h.raw(JSON.stringify({ type: 'ack' }));
  t.mock.timers.tick(200);
  assert.equal(h.events.length, 8);
  assert.deepEqual(h.invalidations.sort(), ['activities', 'events', 'meetings', 'project-task-wall', 'projects', 'task-wall', 'tasks', 'teams', 'workflows']);
});

for (const [type, prefixes] of Object.entries({
  'team.updated': ['events', 'teams'],
  'task.updated': ['events', 'project-task-wall', 'task-wall', 'tasks', 'teams'],
  'agent.updated': ['activities', 'events', 'teams'],
  'meeting.updated': ['events', 'meetings'],
  'workflow.started': ['events', 'workflows'],
  'project.updated': ['events', 'project-task-wall', 'projects'],
  'cc.tool_use': ['activities', 'events', 'teams'],
  other: ['events'],
})) {
  test(`${type} does not widen query scope`, (t) => {
    const h = harness(t);
    h.emit(type);
    t.mock.timers.tick(200);
    assert.deepEqual(h.invalidations.sort(), prefixes);
  });
}
