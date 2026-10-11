// Build first. Uses a real API and a fresh SQLite database, never the user's DB.
// PLAYWRIGHT_MODULE_PATH=/path/to/playwright/index.js node tests/task-creation.browser.mjs
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const output = process.env.DASHBOARD_ACCEPTANCE_OUTPUT || await mkdtemp(resolve(tmpdir(), 'aiteam-task-acceptance-'));
await mkdir(output, { recursive: true });
const port = process.env.TASK_ACCEPTANCE_PORT || '18082';
const base = `http://127.0.0.1:${port}`;
// Never reuse a running listener or someone else's database.
try { await fetch(`${base}/api/health`); throw new Error(`Port ${port} is already occupied`); }
catch (error) { if (error.message.includes('already occupied')) throw error; }
const database = resolve(await mkdtemp(resolve(tmpdir(), 'aiteam-task-db-')), 'acceptance.db');
const server = spawn(process.env.PYTHON || 'python3', ['dashboard/tests/task-creation-server.py'], {
  cwd: root,
  env: { ...process.env, PYTHONPATH: resolve(root, 'src'), AITEAM_DB_PATH: database, TASK_ACCEPTANCE_PORT: port },
  stdio: ['ignore', 'pipe', 'pipe'],
});
const serverClosed = new Promise((done) => server.once('close', done));
let serverLog = '';
server.stdout.on('data', (chunk) => { serverLog += chunk; });
server.stderr.on('data', (chunk) => { serverLog += chunk; });
let browser;
const results = [];
async function api(path) {
  const response = await fetch(base + path);
  assert.equal(response.ok, true, `GET ${path}: ${response.status}`);
  return response.json();
}
async function eventually(check) {
  const deadline = Date.now() + 15000;
  let last;
  while (Date.now() < deadline) {
    try { return await check(); } catch (error) { last = error; }
    await new Promise((done) => setTimeout(done, 100));
  }
  throw last;
}
try {
  await eventually(() => api('/api/health'));
  const projects = (await api('/api/projects')).data;
  const fixture = { empty: projects.find((p) => p.name === 'No teams').id, staffed: projects.find((p) => p.name === 'With teams').id };
  browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, locale: 'en-US' });
  // Keep language deterministic, without changing the user's preferences.
  await page.route('**/api/settings/language?*', (route) => route.fulfill({
    json: { mode: 'en', effective: 'en', source: 'dashboard' },
  }));
  await page.addInitScript(() => localStorage.setItem('lang', 'en'));
  await page.goto(base + '/tasks');
  const create = page.getByRole('button', { name: 'Create Task', exact: true });
  await create.waitFor();
  await page.getByRole('combobox').first().click();
  await page.getByRole('option', { name: 'No teams', exact: true }).click();
  await page.screenshot({ path: resolve(output, 'no-team-button.png') });
  await eventually(async () => assert.equal(await create.isEnabled(), true, 'Project without teams must allow task creation'));
  results.push('无团队项目创建按钮可用');
  await create.click();
  const dialog = page.getByRole('dialog');
  await dialog.locator('#task-title').fill('   ');
  const submit = dialog.getByRole('button', { name: 'Submit', exact: true });
  assert.equal(await submit.isDisabled(), true);
  assert.equal((await api(`/api/projects/${fixture.empty}/task-wall`)).stats.total, 0);
  results.push('空白标题禁止提交');
  await dialog.locator('#task-title').fill('  Project task  ');
  await dialog.locator('#task-desc').fill('  Saved description  ');
  const savedResponse = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().endsWith(`/api/projects/${fixture.empty}/tasks`));
  await submit.click();
  const saved = await (await savedResponse).json();
  const task = (await api(`/api/tasks/${saved.data.id}`)).data;
  assert.equal(task.project_id, fixture.empty);
  assert.equal(task.team_id, null);
  assert.equal(task.title, 'Project task');
  assert.equal(task.description, 'Saved description');
  assert.equal(task.status, 'pending');
  await page.getByText('Project task', { exact: true }).first().waitFor();
  await page.reload();
  await page.getByRole('combobox').first().click();
  await page.getByRole('option', { name: 'No teams', exact: true }).click();
  await page.getByText('Project task', { exact: true }).first().waitFor();
  await page.screenshot({ path: resolve(output, 'persisted-project-task.png') });
  results.push('无团队创建、即时刷新、页面重载及跨请求持久化正确');

  await page.getByRole('combobox').first().click();
  await page.getByRole('option', { name: 'With teams', exact: true }).click();
  await eventually(async () => assert.equal(await page.getByText('Project task', { exact: true }).count(), 0));
  results.push('切换项目后任务不串项目');
  await create.click();
  await dialog.locator('#task-title').fill('Unassigned project task');
  const unassignedResponse = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().endsWith(`/api/projects/${fixture.staffed}/tasks`));
  await submit.click();
  const unassigned = (await (await unassignedResponse).json()).data;
  assert.equal((await api(`/api/tasks/${unassigned.id}`)).data.team_id, null);
  await page.getByText('Unassigned project task', { exact: true }).first().waitFor();
  results.push('有团队时默认仍可创建项目任务');

  await create.click();
  await dialog.getByRole('combobox').click();
  await page.getByRole('option', { name: 'Team B', exact: true }).click();
  await dialog.locator('#task-title').fill('Team task');
  const teamResponse = page.waitForResponse((r) => r.request().method() === 'POST' && /\/api\/teams\/[^/]+\/tasks\/run$/.test(r.url()));
  await submit.click();
  const teamSaved = (await (await teamResponse).json()).data;
  const teamTask = (await api(`/api/tasks/${teamSaved.id}`)).data;
  assert.equal(teamTask.project_id, fixture.staffed);
  const teams = (await api('/api/teams')).data;
  assert.equal(teamTask.team_id, teams.find((t) => t.name === 'Team B').id);
  assert.equal(teamTask.status, 'pending');
  await page.getByText('Team task', { exact: true }).first().waitFor();
  results.push('显式团队绑定正确，保持待办、不启动执行');

  await create.click();
  await dialog.locator('#task-title').fill('Retry task');
  await dialog.locator('#task-desc').fill('Keep my input');
  await page.route(`**/api/projects/${fixture.staffed}/tasks`, async (route) => {
    if (route.request().method() === 'POST') await route.fulfill({ status: 500, json: { detail: 'Injected failure' } });
    else await route.continue();
  });
  await submit.click();
  await dialog.getByRole('alert').waitFor();
  assert.equal(await dialog.locator('#task-title').inputValue(), 'Retry task');
  assert.equal(await dialog.locator('#task-desc').inputValue(), 'Keep my input');
  await page.unroute(`**/api/projects/${fixture.staffed}/tasks`);
  await submit.click();
  await page.getByText('Retry task', { exact: true }).first().waitFor();
  results.push('失败显示错误、保留输入并可重试');
  for (const team of teams) assert.equal((await api(`/api/teams/${team.id}/agents`)).data.length, 0);
  assert.equal((await api(`/api/projects/${fixture.empty}/task-wall`)).stats.total, 1);
  assert.equal((await api(`/api/projects/${fixture.staffed}/task-wall`)).stats.total, 3);
  await page.getByRole('combobox').first().click();
  await page.getByRole('option', { name: 'One team', exact: true }).click();
  await create.click();
  await dialog.getByRole('combobox').click();
  await page.getByRole('option', { name: 'Only team', exact: true }).click();
  await dialog.locator('#task-title').fill('Single team task');
  const singleResponse = page.waitForResponse((r) => r.request().method() === 'POST' && /\/api\/teams\/[^/]+\/tasks\/run$/.test(r.url()));
  await submit.click();
  const singleTask = (await api(`/api/tasks/${(await (await singleResponse).json()).data.id}`)).data;
  assert.equal(singleTask.team_id, teams.find((t) => t.name === 'Only team').id);
  assert.equal(singleTask.project_id, projects.find((p) => p.name === 'One team').id);
  await page.getByText('Single team task', { exact: true }).first().waitFor();
  results.push('仅一个团队时也可显式选择并绑定团队');
  console.log(JSON.stringify({ results, database, output }, null, 2));
} finally {
  await browser?.close();
  server.kill('SIGTERM');
  await serverClosed;
  await writeFile(resolve(output, 'server.log'), serverLog);
  await writeFile(resolve(output, 'results.json'), JSON.stringify({ results, database }, null, 2));
}
