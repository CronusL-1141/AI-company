// The workflow agent state badge speaks the Dashboard language for every state the
// Claude Code Workflow JSON writes; an unknown state is shown as it came.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function compile(relative, mocks = {}) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
  const code = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
  const exports = {};
  new Function('require', 'exports', code)((name) => mocks[name] ?? require(name), exports);
  return exports;
}

const languages = {
  zh: compile('../src/i18n/zh.ts').zh,
  en: compile('../src/i18n/en.ts', { './zh': {} }).en,
};
let translations = languages.zh;
const { AgentStateBadge } = compile('../src/components/workflows/AgentStateBadge.tsx', {
  '@/components/ui/badge': { Badge: ({ children, className }) => React.createElement('span', { className }, children) },
  '@/lib/utils': { cn: (...names) => names.filter(Boolean).join(' ') },
  '@/i18n': { useT: () => translations },
});

function badge(state, lang) {
  translations = languages[lang];
  const markup = renderToStaticMarkup(React.createElement(AgentStateBadge, { state }));
  return { text: markup.replace(/<[^>]+>/g, ''), red: markup.includes('text-red-700') };
}

// Stored runs carry start, running, progress, done and error (a failed agent is
// "error"); queued comes before the first start; failed is kept as a synonym.
const STATES = ['queued', 'start', 'running', 'progress', 'done', 'error', 'failed'];

test('every state the workflow JSON writes has a label in both languages', () => {
  for (const lang of ['zh', 'en']) {
    for (const state of STATES) {
      const { text } = badge(state, lang);
      assert.ok(text && text !== state, `${lang}: "${state}" is shown raw`);
    }
  }
});

test('an error agent (how stored runs record a failure) reads as failed, in red', () => {
  for (const state of ['error', 'failed']) {
    assert.deepEqual(badge(state, 'zh'), { text: '失败', red: true });
    assert.deepEqual(badge(state, 'en'), { text: 'Failed', red: true });
  }
  for (const state of ['queued', 'start', 'running', 'progress', 'done']) {
    assert.equal(badge(state, 'zh').red, false, state);
  }
});

test('an unknown state is shown as it came, and object keys are not states', () => {
  assert.equal(badge('paused', 'zh').text, 'paused');
  assert.equal(badge('toString', 'en').text, 'toString');
});
