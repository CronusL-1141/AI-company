import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
function compile(source, mocks = {}) {
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const exports = {};
  new Function('require', 'exports', code)((name) => mocks[name] ?? require(name), exports);
  return exports;
}
const read = (relative) => readFileSync(new URL(relative, import.meta.url), 'utf8');
const { resolveWorkflowAgentStatus } = compile(read('../src/lib/workflowAgentStatus.ts'));
const offlineAgent = { status: 'offline', role: 'workflow-subagent' };
const languages = { zh: compile(read('../src/i18n/zh.ts')).zh,
  en: compile(read('../src/i18n/en.ts'), { './zh': {} }).en };
let translations = languages.zh;
const Badge = ({ children, className }) => React.createElement('span', { className }, children);
const { AgentStateBadge } = compile(read('../src/components/workflows/AgentStateBadge.tsx'), {
  '@/components/ui/badge': { Badge }, '@/lib/utils': { cn: (...names) => names.filter(Boolean).join(' ') },
  '@/i18n': { useT: () => translations },
});
// Compile the actual local component declarations without unrelated page queries.
function pageBadge(relative, name) {
  const source = read(relative);
  const ast = ts.createSourceFile(relative, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  const fn = ast.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(fn, name);
  const code = ts.transpileModule(fn.getText(ast), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  return new Function('require', 'exports', 'useT', 'Badge', 'AgentStateBadge', code + `; return ${name};`)(
    require, {}, () => translations, Badge, AgentStateBadge);
}
const badges = [pageBadge('../src/pages/ProjectDetailPage.tsx', 'AgentStatusBadge')];

{
  const expected = { error: 'error', failed: 'error', done: 'done', running: 'busy',
    progress: 'busy', start: 'busy', queued: 'waiting' };
  for (const [state, status] of Object.entries(expected)) {
    test(`observed ${state} overrides the process status`, () => {
      for (const raw of ['offline', 'busy', 'waiting']) {
        assert.equal(resolveWorkflowAgentStatus({ ...offlineAgent, status: raw }, state), status, `${state}/${raw}`);
      }
    });
  }
}

test('missing observations retain the existing role and process-status fallback', () => {
  assert.equal(resolveWorkflowAgentStatus(offlineAgent, undefined), 'done');
  assert.equal(resolveWorkflowAgentStatus({ status: 'OFFLINE', role: 'reviewer' }, undefined), 'offline');
  assert.equal(resolveWorkflowAgentStatus({ status: 'BUSY', role: 'workflow-subagent' }, undefined), 'busy');
  assert.equal(resolveWorkflowAgentStatus({ status: 'waiting', role: 'reviewer' }, undefined), 'waiting');
  assert.equal(resolveWorkflowAgentStatus(offlineAgent, 'unknown-future-state'), 'done');
});

test('project agent board shows observed failures in red and in the chosen language', () => {
  for (const lang of ['zh', 'en']) {
    translations = languages[lang];
    for (const state of ['error', 'failed']) {
      const status = resolveWorkflowAgentStatus(offlineAgent, state);
      for (const Component of badges) {
        const markup = renderToStaticMarkup(React.createElement(Component, { status, context: 'member' }));
        assert.equal(markup.replace(/<[^>]+>/g, ''), lang === 'zh' ? '失败' : 'Failed');
        assert.match(markup, /text-red-700/);
        assert.doesNotMatch(markup, /emerald/);
      }
    }
  }
});

test('completed, running and queued badge labels and colors remain compatible', () => {
  for (const lang of ['zh', 'en']) {
    translations = languages[lang];
    for (const [state, key] of [['done', 'done'], ['running', 'busy'], ['queued', 'waiting']]) {
      for (const Component of badges) {
        const markup = renderToStaticMarkup(React.createElement(Component, {
          status: resolveWorkflowAgentStatus(offlineAgent, state), context: 'member',
        }));
        assert.equal(markup.replace(/<[^>]+>/g, ''), translations.agentStatus[key]);
        assert.doesNotMatch(markup, /text-red-700/);
        if (state === 'done') assert.match(markup, /emerald/);
      }
    }
  }
});

test('shared workflow badges retain raw error and failed state labels and red tone', () => {
  for (const lang of ['zh', 'en']) {
    translations = languages[lang];
    for (const state of ['error', 'failed']) {
      const markup = renderToStaticMarkup(React.createElement(AgentStateBadge, { state }));
      assert.equal(markup.replace(/<[^>]+>/g, ''), translations.workflows.agentState[state]);
      assert.match(markup, /text-red-700/);
      assert.doesNotMatch(markup, /emerald|text-green/);
    }
  }
});
