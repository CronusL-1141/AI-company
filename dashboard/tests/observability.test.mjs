import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const ts = require('typescript');
const SRC = fileURLToPath(new URL('../src/', import.meta.url));

const team = (id, projectId = 'project-one') => ({ id, name: id, description: '',
  project_id: projectId, status: 'active', config: {}, max_agents: 20, created_at: '2026-09-10T10:00:00Z' });
const agent = (id, teamId, extra = {}) => ({ id, team_id: teamId, name: id, role: 'worker',
  status: 'busy', model: '', system_prompt: '', config: {}, created_at: '2026-09-10T10:00:00Z',
  last_active_at: new Date().toISOString(), ...extra });
const status = (owner, agents, activeTasks = []) => ({ team: owner, agents,
  active_tasks: activeTasks, completed_tasks: 0, pending_meetings: 0 });
const overview = (count, agents = 1) => ({ total_activities: count, total_agents: agents,
  active_agents: agents, tool_distribution: [{ tool_name: 'Bash', count }],
  agent_productivity: [] });

function harness(options = {}) {
  const teams = options.teams ?? [team('team-one'), team('team-two')];
  const statuses = options.statuses ?? {
    'team-one': status(teams[0], [agent('one', 'team-one')]),
    'team-two': status(teams[1], [agent('two', 'team-two'), agent('three', 'team-two')]),
  };
  const overviews = options.overviews ?? { 'team-one': overview(10), 'team-two': overview(20, 2) };
  const queries = [];
  const apiCalls = [];
  const projects = options.projects ?? [{ id: 'project-one', name: 'Project One',
    root_path: '/test/project-one', description: '', created_at: '2026-09-10T10:00:00Z' }];
  let stateIndex = 0;
  let translations;
  const ui = new Proxy({}, { get: (_, name) => ({ children, className, role, id }) => {
    const tag = name === 'Card' ? 'section' : name === 'CardTitle' ? 'h2' : name === 'Input' ? 'input' : 'div';
    return React.createElement(tag, { className, role, id, 'data-ui': String(name) }, children);
  } });
  const snapshot = (data, error) => ({ data, error: error ?? null, isLoading: false, isError: Boolean(error) });
  const hooks = {
    useQuery: (query) => {
      queries.push(query);
      return options.queryResult?.(query) ?? snapshot(options.queryData);
    },
    useQueries: ({ queries: items }) => items.map((query) => {
      queries.push(query);
      if (query.queryKey.at(-1) === 'status') return snapshot({ success: true, data: statuses[query.queryKey[1]] });
      if (query.queryKey.includes('task-wall')) return snapshot({ stats: { total: 0, completed_count: 0 } });
      return options.queryResult?.(query) ?? snapshot({ data: [] });
    }),
    useQueryClient: () => ({ invalidateQueries() {} }),
    useMutation: () => ({ mutate() {}, isPending: false }),
  };
  const mocks = {
    react: { ...React, useMemo: (fn) => fn(), useCallback: (fn) => fn,
      useState: (initial) => [options.state?.[stateIndex++] ?? initial, () => {}] },
    'react-router-dom': { Link: ({ children, to }) => React.createElement('a', { href: to }, children),
      useParams: () => options.params ?? {}, useNavigate: () => () => {} },
    'lucide-react': new Proxy({}, { get: () => () => null }),
    '@tanstack/react-query': hooks,
    '@/i18n': { useT: () => translations, LanguageContext: React.createContext(null) },
    '@/api/client': { apiFetch: async (url) => {
      apiCalls.push(url);
      if (options.apiFetch) return options.apiFetch(url);
      throw new Error('No test response for ' + url);
    } },
    '@/api/teams': { useTeams: () => snapshot({ data: teams }, options.teamsError) },
    '@/api/projects': { useProjects: () => snapshot({ data: projects }, options.projectsError) },
    '@/api/events': { useEvents: (filters) => {
      apiCalls.push(filters);
      return snapshot({ data: options.events ?? [] }, options.eventsError);
    }, useFailureEvents: () => snapshot(options.events ?? [], options.eventsError) },
    '@/stores/websocket': { useWSStore: (select) => {
      const value = { connected: false, events: [] };
      return select ? select(value) : value;
    } },
    '@/api/analytics': {
      useTeamOverview: (id) => snapshot(overviews[id]),
      useToolUsage: () => snapshot([{ tool_name: 'Bash', count: 30 }]),
      useAgentProductivity: () => snapshot([]), useActivityTimeline: () => snapshot([]),
      useEfficiencyMetrics: () => snapshot(undefined),
    },
    ...options.mocks,
  };
  const cache = new Map();
  function load(path) {
    let absolute = path.startsWith('@/') ? resolve(SRC, path.slice(2)) : resolve(SRC, path);
    absolute = ['', '.ts', '.tsx', '/index.ts'].map((suffix) => absolute + suffix).find(existsSync);
    assert.ok(absolute, `Missing source: ${path}`);
    if (cache.has(absolute)) return cache.get(absolute).exports;
    const output = ts.transpileModule(readFileSync(absolute, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
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
  translations = load('i18n/zh.ts').zh;
  return { load, queries, apiCalls, teams, statuses, overviews, t: translations,
    render: (path, component) => renderToStaticMarkup(React.createElement(load(path)[component])) };
}

test('project cards aggregate agents from every team in the same project', () => {
  const h = harness();
  const html = h.render('pages/DashboardPage.tsx', 'DashboardPage');
  assert.ok(html.includes(h.t.dashboard.agentsWorking(3)), html);
});

test('analytics all-team headline does not use only the first team', () => {
  const h = harness();
  const html = h.render('pages/AnalyticsPage.tsx', 'AnalyticsPage');
  const cards = html.match(/<section\b[\s\S]*?<\/section>/g) ?? [];
  const activities = cards.find((card) => card.includes(h.t.analytics.totalActivities));
  assert.match(activities, />30<\/div>/);
});

test('project grouping never mixes another project into the card', () => {
  const teams = [team('team-one'), team('team-two'), team('foreign', 'project-other')];
  const h = harness({ teams, statuses: {
    'team-one': status(teams[0], [agent('one', 'team-one')]),
    'team-two': status(teams[1], [agent('two', 'team-two')]),
    foreign: status(teams[2], [agent('foreign', 'foreign')]),
  } });
  const html = h.render('pages/DashboardPage.tsx', 'DashboardPage');
  assert.ok(html.includes(h.t.dashboard.agentsWorking(2)));
  assert.ok(!html.includes(h.t.dashboard.agentsWorking(3)));
});

test('analytics queries send identical project/team scope for headline and charts', async () => {
  const h = harness({ apiFetch: async () => ({ success: true, data: [] }) });
  const api = h.load('api/analytics.ts');
  api.useToolUsage('team-one', 'project-one');
  api.useAgentProductivity('team-one', 'project-one');
  api.useActivityTimeline('team-one', 24, 'project-one');
  api.useEfficiencyMetrics('team-one', 'project-one');
  assert.equal(h.queries.length, 4);
  await Promise.all(h.queries.map((query) => query.queryFn()));
  for (const value of h.apiCalls) {
    const url = new URL(value, 'http://test.invalid');
    assert.equal(url.searchParams.get('team_id'), 'team-one');
    assert.equal(url.searchParams.get('project_id'), 'project-one');
    assert.ok(!url.pathname.includes('team-overview'));
  }
});

test('event family selection uses dotted type_prefix and preserves exact-type callers', async () => {
  const h = harness({ state: ['cc'], apiFetch: async () => ({ data: [], total: 0 }) });
  h.render('pages/EventsPage.tsx', 'EventsPage');
  assert.equal(h.apiCalls[0].type_prefix, 'cc.');
  assert.equal(h.apiCalls[0].type, undefined);
  const api = h.load('api/events.ts');
  api.useEvents({ type_prefix: 'cc.', project_id: 'project-one' });
  await h.queries.at(-1).queryFn();
  assert.match(h.apiCalls.at(-1), /type_prefix=cc\./);
  assert.ok(!new URL(h.apiCalls.at(-1), 'http://test.invalid').searchParams.has('type'));
  api.useEvents({ type: 'task.failed' });
  await h.queries.at(-1).queryFn();
  assert.equal(new URL(h.apiCalls.at(-1), 'http://test.invalid').searchParams.get('type'), 'task.failed');
});

test('settings uses health version and clearly labels example values and CC-only governance', () => {
  const h = harness({ queryResult: (query) => ({ isLoading: false, error: null,
    data: query.queryKey[0] === 'health' ? { status: 'ok', version: '9.9.9-test' } : undefined }) });
  const html = h.render('pages/SettingsPage.tsx', 'SettingsPage');
  assert.ok(html.includes('v9.9.9-test'));
  assert.ok(!html.includes('v1.6.2'));
  assert.ok(html.includes(h.t.settings.infraExampleNotice));
  assert.ok(html.includes(h.t.settings.modelGovTitle));
  assert.ok(h.t.settings.modelGovDesc.includes('Codex'));
  assert.ok(h.queries.some((q) => q.queryKey[0] === 'health'));
});

test('failed health version is visible instead of a fabricated version', () => {
  const h = harness({ queryResult: (query) => ({ isLoading: false,
    error: query.queryKey[0] === 'health' ? new Error('health unavailable') : null }) });
  const html = h.render('pages/SettingsPage.tsx', 'SettingsPage');
  assert.ok(html.includes('health unavailable'));
  assert.ok(!html.includes('v1.6.2'));
});

test('generic leader names are readable and missing harness is not inferred as CC', () => {
  const h = harness();
  const { readableAgentName, readableHarness } = h.load('lib/agentPresentation.ts');
  const root = agent('root', 'team-one', { name: 'cc-leader', role: 'leader', session_id: '01a066a0-rest', harness: null });
  assert.equal(readableAgentName(root, 'Main session', 'Unnamed'), 'Main session · 01a066a0');
  assert.equal(readableHarness(root.harness, 'Unlabelled'), 'Unlabelled');
  assert.equal(readableHarness('codex', 'Unlabelled'), 'Codex');
  assert.equal(readableAgentName({ ...root, name: 'Human name' }, 'Main session', 'Unnamed'), 'Human name');
});

test('leader provenance uses harness enum and native member names are not replaced by task labels', () => {
  const { agentKindLabel, readableMemberName } = harness().load('lib/agentPresentation.ts');
  assert.equal(agentKindLabel({ role: 'leader', harness: 'codex' }, 'Unknown'), 'Codex Leader');
  assert.equal(agentKindLabel({ role: 'leader', harness: 'claude-code' }, 'Unknown'), 'Claude Leader');
  assert.equal(agentKindLabel({ role: 'leader', name: 'codex-session', harness: null }, 'Unknown'), 'Unknown');
  for (const name of ['Sagan', 'Poincare', 'Ohm', 'Russell']) {
    const member = { name, role: 'reviewer', harness: 'codex' };
    assert.equal(readableMemberName(member, 'task-template', 'Leader', 'Unnamed'), name);
    assert.equal(agentKindLabel(member, 'Unknown'), 'Codex');
  }
});

test('AgentLive current area contains only busy agents grouped under their team', () => {
  const teams = [team('root-team')];
  const h = harness({ teams, statuses: { 'root-team': status(teams[0], [
    agent('root', 'root-team', { role: 'leader', harness: 'codex', name: 'Main root' }),
    agent('child', 'root-team', { harness: 'codex', name: 'Sagan', role: 'reviewer', current_task: 'Review task' }),
    agent('waiting', 'root-team', { name: 'WAITING_SHOULD_NOT_RENDER', status: 'waiting' }),
    agent('closed', 'root-team', { name: 'OFFLINE_SHOULD_NOT_RENDER', status: 'offline' }),
  ]) } });
  const html = h.render('pages/AgentLivePage.tsx', 'AgentLivePage');
  assert.ok(html.includes('data-team-id="root-team"'));
  assert.ok(html.includes('Codex Leader'));
  assert.ok(html.includes('Sagan') && html.includes('reviewer') && html.includes('Review task'));
  assert.ok(!html.includes('WAITING_SHOULD_NOT_RENDER'));
  assert.ok(!html.includes('OFFLINE_SHOULD_NOT_RENDER'));
  assert.ok(!html.includes('TUI') && !html.includes('vscode'));
});

test('current work requires valid evidence newer than fifteen minutes and folds stale busy records', () => {
  const now = Date.now();
  const teams = [team('freshness-team')];
  const records = [
    agent('fresh-leader', teams[0].id, { role: 'leader', harness: 'codex', name: 'Codex Leader',
      last_active_at: new Date(now - 60_000).toISOString() }),
    agent('old-leader', teams[0].id, { role: 'leader', name: 'STALE_BUSY_LEADER',
      last_active_at: new Date(now - 11 * 60 * 60_000).toISOString() }),
    agent('old-member', teams[0].id, { name: 'STALE_BUSY_MEMBER',
      last_active_at: new Date(now - 15 * 60_000).toISOString() }),
    agent('future', teams[0].id, { name: 'FUTURE_WORK', last_active_at: new Date(now + 60_000).toISOString() }),
    agent('unknown', teams[0].id, { name: 'UNKNOWN_WORK', last_active_at: null }),
    agent('invalid', teams[0].id, { name: 'INVALID_WORK', last_active_at: 'not-a-time' }),
    agent('waiting', teams[0].id, { name: 'WAITING_WORK', status: 'waiting' }),
  ];
  const options = { teams, statuses: { [teams[0].id]: status(teams[0], records) } };
  const h = harness(options);
  const html = h.render('pages/AgentLivePage.tsx', 'AgentLivePage');
  assert.ok(html.includes('Codex Leader'));
  for (const record of records.slice(1)) assert.ok(!html.includes(record.name), record.name);
  assert.ok(!html.includes('Codex Leader · Codex Leader'));
  const expanded = harness({ ...options, state: ['__all__', true] }).render('pages/AgentLivePage.tsx', 'AgentLivePage');
  assert.ok(expanded.includes('STALE_BUSY_LEADER') && expanded.includes('WAITING_WORK'));
  assert.equal(records[1].status, 'busy');

  const { isFreshWorking } = h.load('lib/agentPresentation.ts');
  assert.equal(isFreshWorking({ status: 'busy', last_active_at: new Date(now).toISOString() }, now), true);
  assert.equal(isFreshWorking({ status: 'busy', last_active_at: new Date(now - 899_999).toISOString() }, now), true);
  assert.equal(isFreshWorking({ status: 'busy', last_active_at: new Date(now - 900_000).toISOString() }, now), false);
  assert.equal(isFreshWorking({ status: 'busy', last_active_at: new Date(now + 1).toISOString() }, now), false);
  assert.equal(isFreshWorking({ status: 'busy', last_active_at: undefined }, now), false);
});

test('workflow network errors reject instead of being converted to empty lists', async () => {
  const h = harness({ apiFetch: async () => { throw new Error('network unavailable'); } });
  const api = h.load('api/workflows.ts');
  api.useWorkflows();
  await assert.rejects(h.queries.at(-1).queryFn, /network unavailable/);
  api.useWorkflowAgents('wf-test');
  await assert.rejects(h.queries.at(-1).queryFn, /network unavailable/);
});

test('failure queries include current and legacy event names without a new endpoint', async () => {
  const h = harness({ apiFetch: async () => ({ data: [], total: 0 }) });
  h.load('api/events.ts').useFailureEvents('project-one');
  await Promise.all(h.queries.map((query) => query.queryFn()));
  assert.deepEqual(h.apiCalls.map((value) => new URL(value, 'http://test.invalid').searchParams.get('type')).sort(),
    ['failure_analysis', 'task.failure_analyzed', 'task.failed', 'task_failed'].sort());
  assert.ok(h.apiCalls.every((value) => value.startsWith('/api/events?')));
});

test('failure and prompt request errors do not render zero success states', () => {
  const failure = harness({ eventsError: new Error('failure feed unavailable') });
  assert.ok(failure.render('pages/FailuresPage.tsx', 'FailuresPage').includes('role="alert"'));
  const prompt = harness({ queryResult: () => ({ isLoading: false, error: new Error('prompt unavailable') }) });
  const html = prompt.render('pages/PromptsPage.tsx', 'PromptsPage');
  assert.ok(html.includes('role="alert"'));
  assert.ok(html.includes('prompt unavailable'));
  assert.ok(!html.includes('>0</p>'));
});
