import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const ts = require('typescript');
function management() {
  const code = ts.transpileModule(readFileSync(new URL('../src/components/tasks/TaskManagementFields.tsx', import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const exports = {};
  new Function('exports', 'require', code)(exports, () => ({}));
  return exports;
}
test('task relationship and waiting fields clear explicitly, with dependencies normalized and deduplicated', () => {
  const api = management();
  const draft = api.managementDraft({ horizon: 'mid', parent_id: 'p', depends_on: ['a'], waiting_on: { kind: 'user', target_id: '', reason: 'approval' }, status_note: 'old' });
  assert.equal(draft.waiting, 'user');
  assert.deepEqual(api.managementPayload({ ...draft, parent: '', dependencies: '', waiting: 'none', note: '' }), {
    parent_id: null, depends_on: [], horizon: 'mid', waiting_on: null, status_note: '',
  });
  assert.deepEqual(api.managementPayload({ ...draft, dependencies: ' a, b\na\n ', parent: ' p ', note: 'new' }).depends_on, ['a', 'b']);
  assert.deepEqual(api.managementPayload(draft).waiting_on, { kind: 'user', target_id: '', reason: 'approval' });
});
test('structured HTTP conflicts keep current revision and open subtasks available to the form', async () => {
  const source = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8').replaceAll('import.meta.env', '({})');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  for (const detail of [{ message: 'changed', current_revision: 9, current_task: { title: 'latest' } }, { message: 'open child', open_subtasks: [{ id: 'c', title: 'Child' }] }]) {
    const exports = {};
    new Function('exports', 'fetch', 'window', code)(exports, async () => ({ ok: false, status: 409, json: async () => ({ detail }) }), { location: { host: 'fixture' } });
    await assert.rejects(exports.apiFetch('/api/tasks/p', { method: 'PUT' }), (error) => {
      assert.ok(error instanceof exports.ApiHttpError);
      assert.equal(error.status, 409);
      assert.equal(error.message, detail.message);
      assert.deepEqual(error.detail, detail);
      return true;
    });
  }
});

function editorPatch() {
  const code = ts.transpileModule(readFileSync(new URL('../src/lib/task-editor.ts', import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  new Function('exports', code)(exports);
  return exports.taskEditPatch;
}
test('editing only a title omits unchanged fields on the initial write and conflict retry', () => {
  const patch = editorPatch();
  const initial = { title: 'Old', description: 'Description', result: 'Old result', assigned_to: 'A', status: 'pending', parent_id: null, depends_on: ['a', 'b'], horizon: 'mid', waiting_on: null, status_note: 'Note' };
  const values = { ...initial, title: 'My title', depends_on: ['b', 'a'] };
  assert.deepEqual(patch(initial, values), { title: 'My title' });
  // A revision refresh is not an editing baseline: changing this remote object
  // must not add any of its untouched fields to the retained local patch.
  const latest = { ...initial, title: 'Another title', assigned_to: 'B', result: 'New result', waiting_on: { kind: 'user', target_id: '', reason: 'Decision' } };
  assert.deepEqual({ ...latest, ...patch(initial, values) }, { ...latest, title: 'My title' });
});
test('dirty clears remain explicit, unchanged empty fields are omitted', () => {
  const patch = editorPatch();
  const initial = { title: 'Old', description: 'Text', result: 'Result', assigned_to: 'A', status: 'blocked', parent_id: 'p', depends_on: ['a'], horizon: 'mid', waiting_on: { kind: 'user', target_id: '', reason: 'Decision' }, status_note: 'Note' };
  const cleared = { title: 'Old', description: '', result: '', assigned_to: null, status: 'blocked', parent_id: null, depends_on: [], horizon: 'mid', waiting_on: null, status_note: '' };
  assert.deepEqual(patch(initial, cleared), { description: '', result: '', status_note: '', assigned_to: null, parent_id: null, depends_on: [], waiting_on: null });
  assert.deepEqual(patch({ ...initial, ...cleared }, cleared), {});
});

test('successful task feedback includes current warnings and the legacy create warning, without treating a clean response as a warning', () => {
  const code = ts.transpileModule(readFileSync(new URL('../src/lib/task-warnings.ts', import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  new Function('exports', code)(exports);
  const response = { data: { id: 'task', title: 'Saved task' }, message: 'Saved' };
  assert.equal(exports.taskWarningNotice(response), null);
  assert.deepEqual(exports.taskWarningNotice({ ...response, warnings: ['Keep running', 'Keep running'], _warning: 'Similar task' }), {
    taskId: 'task', taskTitle: 'Saved task', messages: ['Keep running', 'Similar task'],
  });
});
