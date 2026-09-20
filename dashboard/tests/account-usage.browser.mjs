// Build first. Uses an existing Playwright install; never installs a browser.
// PLAYWRIGHT_MODULE_PATH=/path/to/@playwright/test/index.js node tests/account-usage.browser.mjs
// All APIs and WebSockets are mocked. No real account, service or database is accessed.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, dirname, extname, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';

const require = createRequire(import.meta.url);
const { chromium, expect } = require(process.env.PLAYWRIGHT_MODULE_PATH || '@playwright/test');
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const dist = resolve(root, 'dashboard/dist');
const output = process.env.DASHBOARD_ACCEPTANCE_OUTPUT || resolve(tmpdir(), 'aiteam-dashboard-acceptance');
await mkdir(output, { recursive: true });

const now = Date.now();
const at = (offset) => new Date(now + offset).toISOString();
const A = 'a'.repeat(64);
const B = 'b'.repeat(64);
const accounts = [
  { account_key: A, label: 'Historical fixture', created_at: at(-86400000) },
  { account_key: B, label: 'Current fixture', created_at: at(-86400000) },
];
function plan(key, window, changes = {}) {
  return { account_key: key, limit_id: 'codex', window_duration_ms: window,
    resets_at: at(window / 2), observed_at: at(-1000), used_percent: 33,
    estimated_total_usd: '2345.67', delta_usd: '23.4567', delta_used_percent: 1,
    start_snapshot_id: key + '-start-' + window, end_snapshot_id: key + '-end-' + window,
    interval_start: at(-3600000), status: 'estimated', source: 'codex_local_logs',
    pricing_mode: 'logged_tier', catalog_version: 'browser-fixture', catalog_sha256: 'c'.repeat(64),
    reason_code: null, ...changes };
}
function spark(key, window, percent) {
  return plan(key, window, { limit_id: 'codex_bengalfox', used_percent: percent,
    estimated_total_usd: null, delta_usd: null, delta_used_percent: null,
    start_snapshot_id: null, interval_start: null, status: 'unavailable',
    pricing_mode: null, catalog_version: null, catalog_sha256: null,
    reason_code: 'bucket_activity_unattributed' });
}
const details = Object.fromEntries(accounts.map((account) => [account.account_key, {
  account, snapshots: [], estimates: [], plan_estimates: [], pricing_plan_estimates: [
    plan(account.account_key, 18000000, { pricing_mode: 'standard_equivalent',
      estimated_total_usd: '345.67', used_percent: 12 }),
    plan(account.account_key, 604800000),
    spark(account.account_key, 18000000, 20), spark(account.account_key, 604800000, 30),
  ],
}]));
const monitors = Object.fromEntries(accounts.map((account) => [account.account_key, {
  account_key: account.account_key, settings: { enabled: true, interval_ms: 300000 }, revision: 1,
  status: 'waiting', runtime_running: true, last_started_at: at(-10000),
  last_finished_at: at(-1000), next_run_at: at(290000), last_error: null,
}]));
const fixtures = { accounts, details, monitors };
// Reuse production schema validation so browser fixtures cannot silently accept
// a response that the real service would reject.
const validation = spawnSync(process.env.PYTHON || 'python3', ['-c', `
import json, sys
from aiteam.types import PricingAccount, PricingMonitorState, PricingPlanCapacityEstimate
fixtures = json.load(sys.stdin)
for value in fixtures['accounts']: PricingAccount.model_validate(value)
for value in fixtures['monitors'].values(): PricingMonitorState.model_validate(value)
for detail in fixtures['details'].values():
    for value in detail['pricing_plan_estimates']: PricingPlanCapacityEstimate.model_validate(value)
print('production fixture schemas passed')
`], { cwd: root, env: { ...process.env, PYTHONPATH: resolve(root, 'src') },
  input: JSON.stringify(fixtures), encoding: 'utf8' });
assert.equal(validation.status, 0, validation.stderr);

const requests = [];
const failures = [];
const checkpoints = [];
let current = B;
let offline = false;
let breakBody = false;
let captureHttpFailure = false;
const server = createServer(async (request, response) => {
  const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
  if (breakBody && pathname === '/api/account-usage/' + B) {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.write('{"success":');
    setTimeout(() => response.destroy(), 50);
    return;
  }
  const file = resolve(dist, '.' + (pathname.includes('.') ? pathname : '/index.html'));
  if (!file.startsWith(dist + sep)) { response.writeHead(403).end(); return; }
  try {
    const body = await readFile(file);
    response.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript',
      '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml' })[extname(file)] || 'application/octet-stream');
    response.end(body);
  } catch { response.writeHead(404).end(); }
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, locale: 'zh-CN' });
  await context.routeWebSocket('**/ws/events', () => {});
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== origin) { failures.push('unexpected external request: ' + url.origin); await route.abort(); return; }
    if (!url.pathname.startsWith('/api/')) { await route.continue(); return; }
    requests.push({ path: url.pathname + url.search, method: request.method(), body: request.postDataJSON() });
    const send = (data) => route.fulfill({ json: { success: true, data } });
    if (url.pathname === '/api/health') {
      await route.fulfill({ json: { status: 'healthy', version: 'fixture' } }); return;
    }
    if (offline) { await route.abort('connectionrefused'); return; }
    if (url.pathname === '/api/account-usage') { await send({ accounts, current_account_key: current }); return; }
    if (breakBody && url.pathname === '/api/account-usage/' + B) { await route.continue(); return; }
    if (url.pathname === '/api/account-usage/capture') {
      if (captureHttpFailure) { await route.fulfill({ status: 401, json: { detail: 'Fixture login required' } }); return; }
      await send({ account: details[current].account, snapshots: [] }); return;
    }
    const match = /^\/api\/account-usage\/([ab]{64})(?:\/(monitor|label|plan-anchor\/reset))?$/.exec(url.pathname);
    if (!match) { failures.push('unexpected API: ' + url.pathname); await route.abort(); return; }
    const [, key, action] = match;
    if (!action) { assert.equal(url.search, '?include_pricing=false'); await send(details[key]); return; }
    if (action === 'monitor') {
      if (request.method() === 'PUT') {
        const settings = request.postDataJSON();
        assert.equal(typeof settings.enabled, 'boolean');
        if (settings.interval_ms !== undefined) assert.ok(Number.isInteger(settings.interval_ms)
          && settings.interval_ms >= 30000 && settings.interval_ms <= 1800000);
        monitors[key].settings = { ...monitors[key].settings, ...settings };
        monitors[key].revision += 1;
        monitors[key].status = settings.enabled ? 'waiting' : 'disabled';
      }
      await send(monitors[key]); return;
    }
    if (action === 'label') {
      const { label } = request.postDataJSON();
      assert.ok(typeof label === 'string' && label.trim() && label.length <= 80);
      details[key].account.label = label;
      await send(details[key].account); return;
    }
    // This endpoint is deliberately exercised only while disconnected.
    failures.push('unexpected successful anchor mutation'); await route.abort();
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => failures.push(error.message));
  const selector = page.getByRole('combobox');
  const weekly = page.getByRole('article', { name: 'Codex 周窗口', exact: true });
  const refresh = page.getByRole('button', { name: '刷新已有记录', exact: true });
  const capture = page.getByRole('button', { name: '连接/采样本机账号', exact: true });
  const writes = () => requests.filter((request) => request.method !== 'GET');
  const checkpoint = (name) => { checkpoints.push(name); console.log('PASS ' + name); };

  await page.goto(origin + '/usage/accounts');
  await expect(selector).toHaveValue(B);
  await expect(page.getByRole('article')).toHaveCount(4);
  await expect(weekly).toContainText('$2,345.67');
  await expect(weekly).toContainText('Fast 为标准两倍');
  await expect(page.getByRole('article', { name: 'Codex 5 小时窗口', exact: true })).toContainText('$345.67');
  await expect(page.getByRole('article', { name: 'GPT-5.3-Codex-Spark 周窗口', exact: true })).toContainText('30%');
  assert.equal(writes().length, 0);
  await page.screenshot({ path: resolve(output, '01-current-account.png'), fullPage: true });
  checkpoint('current account defaults, independent quota windows and logged Fast price');

  breakBody = true;
  await refresh.click();
  await expect(page.getByRole('alert').first()).toContainText('暂时无法连接 OS 服务');
  await expect(weekly).toContainText('$2,345.67');
  breakBody = false;
  await expect(page.getByRole('alert')).toHaveCount(0, { timeout: 15000 });
  assert.equal(writes().length, 0);
  checkpoint('a real socket closure during JSON streaming preserves cached data and recovers automatically');

  current = A;
  await expect(selector).toHaveValue(A, { timeout: 15000 });
  assert.equal(writes().length, 0);
  checkpoint('confirmed current-account change is discovered by automatic GET polling');
  await selector.selectOption(B);
  await refresh.click();
  await expect(selector).toHaveValue(B);
  checkpoint('explicit history selection survives current-account refresh');

  offline = true;
  await refresh.click();
  await expect(page.getByRole('alert').first()).toContainText('暂时无法连接 OS 服务');
  await expect(weekly).toContainText('$2,345.67');
  await expect(weekly).toContainText('33%');
  await page.screenshot({ path: resolve(output, '02-disconnected-cache.png'), fullPage: true });
  checkpoint('failed reads retain the selected account, amount and observed percentage');

  await capture.click();
  await expect(capture).toBeEnabled();
  await weekly.getByRole('button', { name: '重置统计起点', exact: true }).evaluate((button) => { button.click(); button.click(); });
  await expect(weekly.getByRole('button', { name: '重置统计起点', exact: true })).toBeEnabled();
  await page.getByText('账号设置与监控', { exact: true }).click();
  await page.getByRole('textbox', { name: '账号别名', exact: true }).fill('Retained fixture draft');
  await page.getByRole('button', { name: '保存备注', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存备注', exact: true })).toBeEnabled();
  await page.getByRole('spinbutton').fill('30');
  await page.getByRole('button', { name: '保存间隔', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存间隔', exact: true })).toBeEnabled();
  assert.equal(writes().length, 4);
  assert.equal(writes().filter((request) => request.path.endsWith('/plan-anchor/reset')).length, 1);
  checkpoint('capture, anchor, label and monitor each issue one failed write; duplicate anchor clicks coalesce');

  details[B].pricing_plan_estimates[1] = plan(B, 604800000, { estimated_total_usd: '3456.78', used_percent: 44 });
  offline = false;
  await expect(weekly).toContainText('$3,456.78', { timeout: 15000 });
  await expect(weekly).toContainText('44%');
  await expect(selector).toHaveValue(B);
  assert.equal(writes().length, 4);
  await expect(page.getByRole('textbox', { name: '账号别名', exact: true })).toHaveValue('Retained fixture draft');
  await expect(page.getByRole('spinbutton')).toHaveValue('30');
  await page.screenshot({ path: resolve(output, '03-automatic-recovery.png'), fullPage: true });
  checkpoint('automatic read recovery updates amounts, preserves drafts and never replays writes');

  await page.getByRole('button', { name: '保存备注', exact: true }).click();
  await expect(page.getByText('账号备注已保存。', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '保存间隔', exact: true }).click();
  await expect.poll(() => monitors[B].settings.interval_ms).toBe(30000);
  await expect(page.getByRole('button', { name: '保存间隔', exact: true })).toBeEnabled();
  await page.getByRole('spinbutton').fill('1800');
  await page.getByRole('button', { name: '保存间隔', exact: true }).click();
  await expect.poll(() => monitors[B].settings.interval_ms).toBe(1800000);
  await expect(page.getByRole('button', { name: '暂停监控', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '暂停监控', exact: true }).click();
  await expect.poll(() => monitors[B].settings.enabled).toBe(false);
  assert.equal(monitors[B].settings.interval_ms, 1800000);
  assert.deepEqual(writes().at(-1).body, { enabled: false });
  checkpoint('explicit write retry works; 30-second and 30-minute settings retain exact milliseconds on pause');

  captureHttpFailure = true;
  await capture.click();
  await expect(page.getByRole('alert').filter({ hasText: 'Fixture login required' })).toBeVisible();
  captureHttpFailure = false;
  await capture.click();
  await expect(selector).toHaveValue(A);
  checkpoint('HTTP errors retain their actual reason; successful explicit capture selects its returned account');

  current = B;
  await expect(selector).toHaveValue(B, { timeout: 15000 });
  await expect(weekly).toContainText('$3,456.78');
  checkpoint('capture of account A still follows a later confirmed login to account B');
  await selector.selectOption(A);
  await refresh.click();
  await expect(selector).toHaveValue(A);
  checkpoint('manually selected history after capture remains selected when the current account differs');

  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({ path: resolve(output, '04-mobile.png'), fullPage: true });
  checkpoint('mobile viewport has no page-level horizontal overflow');
  await page.evaluate(() => localStorage.setItem('lang', 'en'));
  await page.reload();
  await expect(page.getByRole('article', { name: 'Codex Weekly window', exact: true })).toContainText('Fast is 2× standard');
  await page.screenshot({ path: resolve(output, '05-english.png'), fullPage: true });
  checkpoint('English logged-tier label preserves the same server-side amount');
  assert.deepEqual(failures, []);
  await writeFile(resolve(output, 'results.json'), JSON.stringify({
    schemaValidation: validation.stdout.trim(), checkpoints, failures,
    apiBoundary: 'Mock API and WebSockets; isolated ephemeral loopback static server; no real account switching',
    requests,
  }, null, 2));
  console.log(JSON.stringify({ passed: checkpoints.length, failures, output }));
} finally {
  await browser?.close();
  await new Promise((done) => server.close(done));
}
