// The select trigger shows the chosen item's text, never its raw value.
// Base UI prints the value ("__all__", a project id) unless the root is given
// item labels; the shared Select collects them from its SelectItem children.
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

const ui = compile('../src/components/ui/select.tsx', {
  '@/lib/utils': { cn: (...names) => names.filter(Boolean).join(' ') },
});
const h = React.createElement;

/** The text inside the trigger's value slot. */
function triggerText(select) {
  const markup = renderToStaticMarkup(select);
  const match = /data-slot="select-value"[^>]*>(.*?)<\/span>/s.exec(markup);
  assert.ok(match, `no value slot in ${markup}`);
  return match[1].replace(/<[^>]+>/g, '');
}

function projectSelect(value, extra = {}) {
  return h(ui.Select, { value, onValueChange: () => {}, ...extra },
    h(ui.SelectTrigger, null, h(ui.SelectValue, { placeholder: 'Pick a project' })),
    h(ui.SelectContent, null,
      h(ui.SelectItem, { value: '__all__' }, 'All projects'),
      h(ui.SelectGroup, null,
        ['Aurora Web Platform', 'Billing'].map((name, i) => h(ui.SelectItem, { key: name, value: `p${i}` }, name))),
    ));
}

test('the "all" sentinel shows its label', () => {
  assert.equal(triggerText(projectSelect('__all__')), 'All projects');
});

test('a selected id shows the item name, also from mapped and grouped items', () => {
  assert.equal(triggerText(projectSelect('p0')), 'Aurora Web Platform');
  assert.equal(triggerText(projectSelect('p1')), 'Billing');
});

test('an item label prop wins over extra item content', () => {
  const select = h(ui.Select, { value: 'gpt-5.5', onValueChange: () => {} },
    h(ui.SelectTrigger, null, h(ui.SelectValue, null)),
    h(ui.SelectContent, null,
      h(ui.SelectItem, { value: 'gpt-5.5', label: 'gpt-5.5' }, 'gpt-5.5', h('span', null, 'used in 3 sessions'))));
  assert.equal(triggerText(select), 'gpt-5.5');
});

test('explicit items and SelectValue children keep precedence', () => {
  assert.equal(triggerText(projectSelect('p0', { items: { p0: 'Explicit' } })), 'Explicit');
  const rendered = h(ui.Select, { value: 'zh', onValueChange: () => {} },
    h(ui.SelectTrigger, null, h(ui.SelectValue, null, (value) => `lang:${value}`)),
    h(ui.SelectContent, null, h(ui.SelectItem, { value: 'zh' }, 'Chinese')));
  assert.equal(triggerText(rendered), 'lang:zh');
});

test('an unknown value still falls back to the raw value, and no value to the placeholder', () => {
  assert.equal(triggerText(projectSelect('p9')), 'p9');
  assert.equal(triggerText(projectSelect(null)), 'Pick a project');
});
