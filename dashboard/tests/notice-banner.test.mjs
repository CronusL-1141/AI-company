import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function compile(relative, mocks = {}) {
  const code = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), 'utf8'), {
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
let language = 'zh';
let summary;
const { NoticeBanner } = compile('../src/components/layout/NoticeBanner.tsx', {
  'react-router-dom': { Link: ({ children }) => React.createElement('a', { href: '/briefings' }, children) },
  '@/components/ui/button': { Button: ({ children }) => React.createElement('button', {}, children) },
  '@/api/notices': {
    noticeText: (line) => line.replace(/^\[AI Team OS\] /, ''),
    useNoticeSummary: () => ({ data: summary }),
    useNoticeAction: () => ({ isPending: false, mutate: () => {} }),
  },
  '@/i18n': { useLang: () => language, useT: () => languages[language] },
});

function render(data, lang) {
  summary = data;
  language = lang;
  return renderToStaticMarkup(React.createElement(NoticeBanner));
}

for (const lang of ['zh', 'en']) {
  const top = { key: 'decisions_pending:test', user_line: lang === 'zh'
    ? '[AI Team OS] 有 3 项等你决定' : '[AI Team OS] 3 decisions are waiting for you' };
  test(`${lang}: an aggregate covering all three decisions has no extra count`, () => {
    const html = render({ top, total: 3, top_count: 3, others: 0 }, lang);
    assert.ok(html.includes(lang === 'zh' ? '有 3 项等你决定' : '3 decisions are waiting for you'));
    assert.ok(!html.includes(languages[lang].notices.bannerMore(2)));
    assert.ok(!html.includes(languages[lang].notices.bannerMore(0)));
  });
  test(`${lang}: an independent notice adds exactly one other item`, () => {
    const html = render({ top, total: 4, top_count: 3, others: 1 }, lang);
    assert.ok(html.includes(languages[lang].notices.bannerMore(1)));
    assert.ok(!html.includes(languages[lang].notices.bannerMore(3)));
  });
  test(`${lang}: an ordinary notice represents one item and no top hides the banner`, () => {
    const ordinary = { key: 'api_version_stale:test', user_line: '[AI Team OS] Update API' };
    assert.ok(render({ top: ordinary, total: 4, top_count: 1, others: 3 }, lang)
      .includes(languages[lang].notices.bannerMore(3)));
    assert.equal(render({ top: null, total: 3, top_count: 0, others: 3 }, lang), '');
  });
}
