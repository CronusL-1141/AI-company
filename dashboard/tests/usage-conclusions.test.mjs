import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const ts = require('typescript');

function compile(relative, mocks = {}) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
  const code = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText;
  const exports = {};
  new Function('require', 'exports', code)((name) => mocks[name] ?? require(name), exports);
  return exports;
}

for (const lang of ['zh', 'en']) {
  test(`${lang}: actual drawer renders terminal conclusions, reason codes and registered paths`, () => {
    const dictionary = compile(`../src/i18n/${lang}.ts`)[lang];
    const usage = compile('../src/api/usage.ts', { './client': { apiFetch: () => {} } });
    const card = ({ children }) => React.createElement('div', null, children);
    const { UnattributedDrawer } = compile('../src/components/usage/UnattributedDrawer.tsx', {
      '@/components/ui/card': Object.fromEntries(
        ['Card', 'CardContent', 'CardDescription', 'CardHeader', 'CardTitle'].map((key) => [key, card]),
      ),
      '@/lib/utils': { cn: (...classes) => classes.filter(Boolean).join(' ') },
      '@/lib/datetime': { formatDateTimeCompact: (value) => value },
      '@/i18n': { useT: () => dictionary },
      '@/api/usage': usage,
    });
    const ref = { path: 'leader_session', harness: 'codex' };
    const refusal = 'codex_parser_rejected:identity_mismatch';
    const reasons = { [refusal]: 1, codex_rollout_not_found: 1, no_usage_lines: 1, in_flight: 1 };
    const samples = {
      ...ref,
      samples: Object.fromEntries(Object.keys(reasons).map((code) => [code, [{
        agent_id: code, name: `sample ${code}`,
        transcript_path: code === 'no_usage_lines' ? '/fixture/archived_sessions/rollout-empty.jsonl' : '',
      }]])),
    };
    const html = renderToStaticMarkup(React.createElement(UnattributedDrawer, {
      report: { rows: [{ ...ref, unattributed_reasons: reasons }], hops: [] },
      samples, focus: ref, onFocusChange: () => {},
    }));
    assert.ok(html.includes(dictionary.usage.reasonParserRejected('identity_mismatch')));
    assert.ok(html.includes(dictionary.usage.reasonParserRejectedHint));
    assert.ok(html.includes(dictionary.usage.reasonRolloutNotFound));
    assert.ok(html.includes(dictionary.usage.reasonRolloutNotFoundHint));
    assert.ok(html.includes(dictionary.usage.investigate));
    assert.ok(html.includes(dictionary.usage.reasonInFlight));
    assert.ok(html.includes('/fixture/archived_sessions/rollout-empty.jsonl'));
    assert.ok(!html.includes(dictionary.usage.reasonNotMeasured));
    assert.ok(!html.includes(dictionary.usage.reasonNotMeasuredHint));
  });
}
