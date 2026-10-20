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
const project = { id: 'project-one', name: 'Project One', description: '', created_at: '2026-10-01T10:00:00Z' };
const session = (id, state, extra = {}) => ({ name: id, session_id: id, model: 'observed-model',
  harness: 'codex', status: state === 'busy' ? 'busy' : state === 'idle' ? 'waiting' : state,
  activity_state: state, live: state === 'busy', status_source: 'db_observation', current_task: '',
  started_at: '2026-10-01T10:00:00Z', last_active_at: '2026-10-13T10:00:00Z',
  in_flight_tasks: 0, in_flight_task_ids: [], ...extra });

function harness(options = {}) {
  const sessions = options.sessions ?? [session('current-codex-session', 'busy')];
  // Mirrors the summary route: leaders are the live busy sessions, sessions are capped
  // at 20 with every live session kept, session_count is the pre-cap total.
  const summary = options.summary ?? { sessions, session_count: sessions.length, sessions_truncated: false,
    sessions_complete: true, unattributed_running_tasks: 0,
    leaders: sessions.filter((s) => s.live === true && s.activity_state === 'busy') };
  const queries = [];
  const calls = [];
  let translations;
  const result = (data, error = null) => ({ data, error, isLoading: false });
  const mutation = { mutate: () => {}, reset: () => {}, isPending: false };
  const ui = new Proxy({}, { get: (_, name) => ({ children, className, role }) => {
    const tag = name === 'Card' ? 'section' : name === 'CardTitle' ? 'h2' : 'div';
    return React.createElement(tag, { className, role }, children);
  } });
  const mocks = {
    'react-router-dom': { useParams: () => ({ projectId: project.id }), useNavigate: () => () => {},
      Link: ({ children, to }) => React.createElement('a', { href: to }, children) },
    'lucide-react': new Proxy({}, { get: () => () => null }),
    '@tanstack/react-query': { useQuery: (query) => { queries.push(query); return result(undefined); } },
    '@/api/client': { apiFetch: async (url) => { calls.push(url); return summary; } },
    '@/api/projects': { useProject: () => result({ data: project }),
      useProjectSummary: () => ({ ...result(summary, options.error), isLoading: options.loading ?? false }) },
    '@/api/teams': { useTeams: () => result({ data: options.teams ?? [] }) },
    '@/api/workflows': { useWorkflows: () => result([]), useWorkflowAgents: () => result([]),
      useWorkflow: (wfId) => result(options.workflowRuns?.[wfId]) },
    '@/api/agents': { useAgents: () => result({ data: [] }), useDeleteAgent: () => mutation },
    '@/api/tasks': { useRunTask: () => mutation }, '@/api/meetings': { useCreateMeeting: () => mutation },
    '@/api/activities': { useTeamActivities: () => result({ data: [] }) },
    '@/api/decisions': { useDecisions: () => result({ data: [] }), useAgentIntents: () => result({ data: [] }) },
    '@/api/events': { useEvents: () => result({ data: [] }) },
    '@/components/tasks/TaskWarnings': { TaskWarnings: () => null },
    '@/components/tasks/useTaskWriteWarnings': { useTaskWriteWarnings: () => ({
      warningNotice: null, recordWarnings: () => {}, dismissWarnings: () => {} }) },
    '@/components/ecosystem/EcosystemSettingsPanel': { EcosystemSettingsPanel: () => null },
    '@/pages/WorkflowsPage': { fmtDuration: () => '', StatusBadge: () => null },
    '@/components/agents/ActivityLog': {}, '@/components/shared/LiveIndicator': { LiveIndicator: () => null },
    '@/components/shared/ContextWatermarkBar': { ContextWatermarkBar: () => null },
    '@/components/shared/useToast': { useToast: () => ({ showToast: () => {}, toastNode: null }) },
    '@/i18n': { useT: () => translations },
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
  translations = load(`i18n/${options.lang ?? 'zh'}.ts`)[options.lang ?? 'zh'];
  return { t: translations, queries, calls, load,
    render: () => renderToStaticMarkup(React.createElement(load('pages/ProjectDetailPage.tsx').ProjectDetailPage)) };
}

for (const lang of ['zh', 'en']) {
  test(`${lang}: records persist across busy, idle and offline without claiming completion`, () => {
    for (const state of ['busy', 'idle', 'offline', 'unknown']) {
      const h = harness({ lang, sessions: [session('codex-transition-record', state,
        state === 'idle' ? { status: 'busy', status_source: 'codex_rollout' } : {})] });
      const html = h.render();
      assert.ok(html.includes('codex-transition-record'), state);
      assert.ok(html.includes('Codex Leader'));
      assert.ok(html.includes(h.t.projectDetail.currentSessions(state === 'busy' ? 1 : 0)));
      if (state !== 'busy') {
        assert.ok(html.includes('<details'));
        assert.ok(html.includes(h.t.projectDetail.historySessions(1)));
        assert.ok(html.includes(state === 'idle' ? h.t.projectDetail.sessionIdle
          : state === 'offline' ? h.t.projectDetail.sessionOffline : h.t.projectDetail.sessionUnknown));
      }
      assert.ok(html.includes(h.t.projectDetail.sessionFirstObserved));
      assert.ok(html.includes(h.t.projectDetail.sessionLastActivity));
      assert.ok(!html.includes(`>${h.t.agentStatus.done}</div>`));
      assert.ok(!html.includes(`>${h.t.agentStatus.finished}</div>`));
    }
  });
}

test('all harness records and independent session count remain visible with a completed session team', () => {
  const sessions = [session('native-codex-history', 'offline'),
    session('claude-idle-history', 'idle', { harness: 'claude-code', status_source: 'cc_registry' }),
    session('unlabelled-history', 'unknown', { harness: null })];
  const h = harness({ sessions, teams: [{ id: 'old-team', name: 'session-0123abcd', project_id: project.id,
    status: 'completed', config: { kind: 'session' }, created_at: project.created_at }] });
  const html = h.render();
  for (const s of sessions) assert.ok(html.includes(s.session_id));
  assert.ok(html.includes('Claude Leader') && html.includes('Codex Leader'));
  assert.ok(html.includes(h.t.projectDetail.sessionsCount));
  assert.match(html, /已记录会话<\/span><span[^>]*>3 /);
  assert.ok(html.includes(h.t.projectDetail.historyTeamsTitle(1)));
  assert.ok(html.includes(h.t.projectDetail.sessionSourceRegistry));
});

test('linked tasks, known zero, unknown and unattributed project tasks have distinct displays', () => {
  const sessions = [session('known-zero', 'busy'), session('unknown-linkage', 'idle', { in_flight_tasks: null }),
    session('known-linkage', 'busy', { in_flight_tasks: 1, in_flight_task_ids: ['task-abcdef1234'] })];
  const h = harness({ summary: { sessions, session_count: 3, sessions_complete: true, unattributed_running_tasks: 4 } });
  const html = h.render();
  assert.ok(html.includes(h.t.projectDetail.unattributedRunningTasks(4)));
  assert.ok(html.includes(h.t.projectDetail.sessionCurrentTaskUnrecorded));
  assert.match(html, /已关联运行任务<\/p><p[^>]*>0<\/p>/);
  assert.ok(html.includes('已关联运行任务: 未知'));
  assert.ok(html.includes('task-abcdef1234'));
  assert.ok(!html.includes('/tasks/task-abcdef1234'));
  const missing = harness({ summary: { sessions, session_count: 3, sessions_complete: true } });
  assert.ok(missing.render().includes(missing.t.projectDetail.unattributedRunningTasksUnknown));
});

test('an unrecorded current-task description does not deny known or unknown running-task attribution', () => {
  for (const lang of ['zh', 'en']) {
    for (const count of [1, null]) {
      const h = harness({ lang, sessions: [session('empty-current-task', 'busy', {
        current_task: '', in_flight_tasks: count, in_flight_task_ids: count === 1 ? ['task-linked'] : [],
      })] });
      const html = h.render();
      assert.ok(html.includes(lang === 'zh' ? '未记录当前任务' : 'Current task not recorded'), `${lang} ${count}`);
      assert.ok(!html.includes(lang === 'zh' ? '未关联任务' : 'No linked task'), `${lang} ${count}`);
      const value = count ?? h.t.projectDetail.sessionUnknown;
      assert.match(html, new RegExp(`${h.t.projectDetail.inFlightTasks}</p><p[^>]*>${value}</p>`), `${lang} ${count}`);
      assert.equal(html.includes('title="task-linked"'), count === 1, `${lang} ${count}`);
    }
  }
});

test('API failure, unsupported sessions and loading do not fabricate zero sessions or no-current-work', () => {
  const cases = [{ error: new Error('summary unavailable') }, { summary: { session_count: 0, leaders: [] } },
    { loading: true, summary: {} }];
  for (const options of cases) {
    const h = harness(options);
    const html = h.render();
    assert.ok(html.includes(options.loading ? h.t.projectDetail.sessionsLoading : h.t.projectDetail.sessionsUnavailable));
    assert.match(html, /已记录会话<\/span><span[^>]*>– /);
    assert.ok(!html.includes(h.t.projectDetail.noLeaderYet));
  }
});

test('partial records show the collection gap while retaining records', () => {
  const h = harness({ summary: { sessions: [session('partial-record', 'unknown')], session_count: 1,
    sessions_complete: false, unattributed_running_tasks: null } });
  const html = h.render();
  assert.ok(html.includes('role="alert"'));
  assert.ok(html.includes(h.t.projectDetail.sessionsIncomplete));
  assert.ok(html.includes('partial-record'));
});

test('stale registry busy is retained as unconfirmed history, never current working', () => {
  for (const lang of ['zh', 'en']) {
    for (const activityState of ['busy', 'unknown']) {
      const h = harness({ lang, sessions: [session('stale-registry-session', activityState, {
        status: 'busy', live: false, status_source: 'cc_registry', last_active_at: '2026-10-01T10:00:00Z',
      })] });
      const html = h.render();
      assert.ok(html.includes('stale-registry-session'));
      assert.ok(html.includes(h.t.projectDetail.currentSessions(0)));
      assert.ok(!html.includes(h.t.projectDetail.currentSessions(1)));
      assert.ok(html.includes(h.t.projectDetail.historySessions(1)));
      assert.ok(html.includes(h.t.projectDetail.sessionStaleBusy));
      assert.ok(html.includes(h.t.projectDetail.sessionSourceRegistry));
      assert.ok(!html.includes(`>${h.t.agentStatus.busy}</div>`));
    }
  }
  const missing = harness({ sessions: [session('missing-live-evidence', 'busy', { live: undefined })] });
  assert.ok(missing.render().includes(missing.t.projectDetail.currentSessions(0)));
});

test('summary hook remains project scoped and periodically refreshes state transitions', async () => {
  const h = harness();
  const api = h.load('api/projects.ts');
  api.useProjectSummary(project.id);
  api.useProjectSummary('');
  const query = h.queries[0];
  assert.deepEqual(query.queryKey, ['projects', project.id, 'summary']);
  assert.equal(query.refetchInterval, 30_000);
  assert.equal(query.enabled, true);
  assert.equal(h.queries[1].enabled, false);
  await query.queryFn();
  assert.equal(h.calls[0], '/api/projects/project-one/summary');
});

// Keep the project page coupled to the real shared display component, so removing
// an obsolete page cannot silently drop workflow names or their tracing IDs.
test('completed workflow teams retain run names and tracing IDs on the project page', () => {
  const h = harness({ teams: [{ id: 'history-workflow', name: 'workflow-observed-run',
    project_id: project.id, status: 'completed', config: { kind: 'workflow' }, created_at: project.created_at }],
    workflowRuns: { 'observed-run': { name: 'Historical review workflow' } } });
  const html = h.render();
  assert.ok(html.includes('Historical review workflow'));
  assert.match(html, /font-mono[^>]*>observed-run<\/span>/);
});

// leaders only carries live busy sessions; an idle owner between turns must keep
// its CEO title on the session team instead of falling back to the raw team name.
test('session team titles resolve the owner from all recorded sessions, not only busy leaders', () => {
  const owner = '0b0b0b0b-0000-4000-8000-00000000b0b0';
  const sessionTeam = (status) => ({ id: `team-${status}`, name: 'session-0b0b0b0b', project_id: project.id,
    status, config: { kind: 'session', owner_session_id: owner }, created_at: project.created_at });
  const titled = (html) => /<span class="inline-flex flex-col leading-tight"><span>CEO-bravo<\/span><span[^>]*>session-0b0b0b0b<\/span>/.test(html);
  for (const lang of ['zh', 'en']) {
    for (const state of ['busy', 'idle', 'offline']) {
      for (const status of ['active', 'completed']) {
        const sessions = [session(owner, state, { name: 'CEO-bravo', harness: 'claude-code', status_source: 'cc_registry' })];
        const h = harness({ lang, sessions, teams: [sessionTeam(status)] });
        const html = h.render();
        assert.ok(titled(html), `${lang} ${state} ${status}`);
      }
    }
  }
  // Older APIs without sessions still resolve through leaders.
  const legacy = harness({ summary: { leaders: [session(owner, 'busy', { name: 'CEO-bravo' })] },
    teams: [sessionTeam('active')] });
  assert.ok(titled(legacy.render()));
  // An unknown owner keeps the raw team name rather than borrowing another session.
  const unmatched = harness({ sessions: [session('ffffffff-other', 'idle', { name: 'CEO-other' })],
    teams: [sessionTeam('active')] });
  const html = unmatched.render();
  assert.ok(!titled(html) && !/<span>CEO-other<\/span><span[^>]*>session-0b0b0b0b/.test(html));
  assert.ok(html.includes('session-0b0b0b0b'));
});

const historySection = (html) => html.match(/<details[\s\S]*?<\/details>/)?.[0] ?? '';

test('a live busy session is shown as current work and never repeated in the history section', () => {
  for (const lang of ['zh', 'en']) {
    const sessions = [session('live-busy-session', 'busy', { harness: 'claude-code', status_source: 'cc_registry' }),
      session('idle-history-session', 'idle', { last_active_at: '2026-10-13T09:00:00Z' })];
    const h = harness({ lang, sessions });
    const html = h.render();
    const history = historySection(html);
    assert.ok(html.includes(h.t.projectDetail.currentSessions(1)));
    assert.ok(html.indexOf('live-busy-session') < html.indexOf('<details'));
    assert.ok(history.includes(h.t.projectDetail.historySessions(1)));
    assert.ok(history.includes('idle-history-session'));
    assert.ok(!history.includes('live-busy-session'));
  }
});

test('a capped session list shows the latest history records out of the pre-cap total', () => {
  const minute = (offset) => new Date(Date.UTC(2026, 9, 13, 10, 0) - offset * 60_000).toISOString();
  const live = [0, 1].map((index) => session(`live-${index}`, 'busy', { last_active_at: minute(index) }));
  const recent = Array.from({ length: 18 }, (_, index) => session(`recent-${index}`, index % 2 ? 'offline' : 'idle',
    { last_active_at: minute(10 + index) }));
  const sessions = [...live, ...recent];
  for (const lang of ['zh', 'en']) {
    const h = harness({ lang, summary: { sessions, session_count: 35, sessions_truncated: true, sessions_complete: true,
      unattributed_running_tasks: 0, leaders: live } });
    const html = h.render();
    const history = historySection(html);
    assert.ok(html.includes(h.t.projectDetail.currentSessions(2)));
    assert.ok(history.includes(h.t.projectDetail.historySessionsTruncated(18, 33)), lang);
    assert.ok(!history.includes(h.t.projectDetail.historySessions(18)));
    assert.match(html, new RegExp(`${h.t.projectDetail.sessionsCount}</span><span[^>]*>35 `));
    for (const s of recent) assert.ok(history.includes(s.session_id));
  }
  const zh = harness({ summary: { sessions, session_count: 35, sessions_truncated: true, sessions_complete: true,
    unattributed_running_tasks: 0, leaders: live } }).render();
  assert.ok(zh.includes('其他会话记录（最近 18 个，共 33 个）'));
  const en = harness({ lang: 'en', summary: { sessions, session_count: 35, sessions_truncated: true,
    sessions_complete: true, unattributed_running_tasks: 0, leaders: live } }).render();
  assert.ok(en.includes('Other Session Records (latest 18 of 33)'));
  // An uncapped list keeps the plain count.
  const full = harness({ sessions });
  const fullHistory = historySection(full.render());
  assert.ok(fullHistory.includes(full.t.projectDetail.historySessions(18)));
  assert.ok(!fullHistory.includes('，共 '));
});

test('every status source the backend emits has its own evidence label', () => {
  const labels = { db_observation: 'sessionSourceDatabase', cc_registry: 'sessionSourceRegistry',
    codex_rollout: 'sessionSourceRollout', activity_only: 'sessionSourceActivity', identity_only: 'sessionSourceIdentity' };
  for (const lang of ['zh', 'en']) {
    for (const [source, key] of Object.entries(labels)) {
      const h = harness({ lang, sessions: [session(`source-${source}`, 'idle', { status_source: source })] });
      const html = h.render();
      assert.ok(html.includes(`${h.t.projectDetail.sessionStateSource}: ${h.t.projectDetail[key]}`), `${lang} ${source}`);
    }
    const h = harness({ lang, sessions: [session('source-unrecognised', 'idle', { status_source: 'future_source' })] });
    assert.ok(h.render().includes(`${h.t.projectDetail.sessionStateSource}: ${h.t.projectDetail.sessionUnknown}`));
  }
});
