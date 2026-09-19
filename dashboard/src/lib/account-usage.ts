import type { PricingQuotaSnapshot, PricingUsageBatch, PricingUsageEntry } from '@/api/accountUsage';
import { epochMsToIso } from '@/lib/datetime';

export interface AccountImportDraft {
  accountKey: string;
  startId: string;
  endId: string;
  raw: string;
  statement: string;
  confirmationKey: string | null;
  confirmedAt: string | null;
}

export type AccountImportAction =
  | { type: 'edit'; field: 'startId' | 'endId' | 'raw' | 'statement'; value: string }
  | { type: 'confirm'; key: string | null; at: string | null }
  | { type: 'saved' };

export function initialAccountImport(accountKey: string): AccountImportDraft {
  return { accountKey, startId: '', endId: '', raw: '', statement: '', confirmationKey: null, confirmedAt: null };
}

export function accountImportReducer(state: AccountImportDraft, action: AccountImportAction): AccountImportDraft {
  if (action.type === 'edit') {
    return { ...state, [action.field]: action.value, confirmationKey: null, confirmedAt: null };
  }
  if (action.type === 'confirm') return { ...state, confirmationKey: action.key, confirmedAt: action.at };
  return { ...state, raw: '', statement: '', confirmationKey: null, confirmedAt: null };
}

export function importConfirmationKey(
  draft: AccountImportDraft,
  start: PricingQuotaSnapshot | undefined,
  end: PricingQuotaSnapshot | undefined,
): string {
  return JSON.stringify([draft.accountKey, start, end, draft.raw, draft.statement]);
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function awareTime(value: unknown): value is string {
  return typeof value === 'string' && /T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    && Number.isFinite(Date.parse(value));
}

function rejectDuplicateJsonKeys(raw: string): void {
  let cursor = 0;
  function whitespace() {
    while (cursor < raw.length && /\s/.test(raw[cursor])) cursor += 1;
  }
  function expect(character: string) {
    whitespace();
    if (raw[cursor] !== character) throw new Error('json');
    cursor += 1;
  }
  function string(): string {
    whitespace();
    const begin = cursor;
    expect('"');
    while (cursor < raw.length) {
      if (raw[cursor] === '\\') { cursor += 2; continue; }
      if (raw[cursor++] === '"') return JSON.parse(raw.slice(begin, cursor)) as string;
    }
    throw new Error('json');
  }
  function value(depth: number) {
    if (depth > 32) throw new Error('json');
    whitespace();
    if (raw[cursor] === '"') { string(); return; }
    if (raw[cursor] === '{') {
      cursor += 1; whitespace();
      const keys = new Set<string>();
      if (raw[cursor] === '}') { cursor += 1; return; }
      while (cursor < raw.length) {
        const key = string();
        if (keys.has(key)) throw new Error('duplicate_key');
        keys.add(key);
        expect(':'); value(depth + 1); whitespace();
        if (raw[cursor] === '}') { cursor += 1; return; }
        expect(',');
      }
      throw new Error('json');
    }
    if (raw[cursor] === '[') {
      cursor += 1; whitespace();
      if (raw[cursor] === ']') { cursor += 1; return; }
      while (cursor < raw.length) {
        value(depth + 1); whitespace();
        if (raw[cursor] === ']') { cursor += 1; return; }
        expect(',');
      }
      throw new Error('json');
    }
    const begin = cursor;
    while (cursor < raw.length && !/[\s,}\]]/.test(raw[cursor])) cursor += 1;
    if (begin === cursor) throw new Error('json');
  }
  value(0); whitespace();
  if (cursor !== raw.length) throw new Error('json');
}

export function parseAccountEntries(raw: string): PricingUsageEntry[] {
  if (raw.length > 2_000_000) throw new Error('size');
  // Decode each key before comparison so escaped spellings cannot overwrite data.
  rejectDuplicateJsonKeys(raw);
  const rows: unknown = JSON.parse(raw);
  if (!Array.isArray(rows) || rows.length < 1 || rows.length > 1000) throw new Error('entries');
  const ids = new Set<string>();
  const fields = ['request_id', 'model', 'service_tier', 'input_tokens', 'output_tokens',
    'cached_input_tokens', 'cache_write_input_tokens'];
  const counters = fields.slice(3);
  const tiers = ['standard', 'default', 'fast', 'priority', 'flex', 'batch'];
  for (const row of rows) {
    if (!object(row) || Object.keys(row).length !== 2 || !awareTime(row.occurred_at) || !object(row.request)) {
      throw new Error('entry');
    }
    const request = row.request;
    if (Object.keys(request).length !== fields.length || fields.some((field) => !(field in request))
      || typeof request.request_id !== 'string' || !request.request_id.trim()
      || typeof request.model !== 'string' || !request.model.trim()
      || !tiers.includes(String(request.service_tier))
      || counters.some((field) => typeof request[field] !== 'number'
        || !Number.isSafeInteger(request[field]) || (request[field] as number) < 0)) {
      throw new Error('request');
    }
    if ((request.cached_input_tokens as number) + (request.cache_write_input_tokens as number)
      > (request.input_tokens as number)) throw new Error('cache');
    if (ids.has(request.request_id)) throw new Error('duplicate');
    ids.add(request.request_id);
  }
  return rows as PricingUsageEntry[];
}

export function compatibleSnapshotPair(start: PricingQuotaSnapshot, end: PricingQuotaSnapshot): boolean {
  return start.snapshot_id !== end.snapshot_id && start.account_key === end.account_key
    && start.limit_id === end.limit_id && start.window_duration_ms === 604_800_000
    && end.window_duration_ms === 604_800_000
    && Date.parse(start.resets_at) === Date.parse(end.resets_at)
    && Date.parse(start.observed_at) < Date.parse(end.observed_at);
}

export function buildAccountBatch(
  draft: AccountImportDraft,
  start: PricingQuotaSnapshot | undefined,
  end: PricingQuotaSnapshot | undefined,
  batchId: string,
): PricingUsageBatch {
  if (!start || !end || start.account_key !== draft.accountKey || !compatibleSnapshotPair(start, end)) {
    throw new Error('interval');
  }
  const entries = parseAccountEntries(draft.raw);
  if (entries.some((entry) => Date.parse(entry.occurred_at) <= Date.parse(start.observed_at)
    || Date.parse(entry.occurred_at) > Date.parse(end.observed_at))) throw new Error('interval');
  const complete = Boolean(draft.confirmedAt && draft.statement.trim()
    && draft.confirmationKey === importConfirmationKey(draft, start, end));
  return {
    batch_id: batchId, account_key: draft.accountKey,
    start_snapshot_id: start.snapshot_id, end_snapshot_id: end.snapshot_id,
    coverage: complete ? 'account_complete' : 'local_only',
    coverage_statement: complete ? draft.statement.trim() : '',
    coverage_confirmed_at: complete ? draft.confirmedAt : null,
    entries,
  };
}

export interface QuotaTrend {
  account_key: string;
  limit_id: string;
  window_duration_ms: number;
  resets_at: string;
  start_snapshot_id: string | null;
  end_snapshot_id: string;
  interval_start: string | null;
  interval_end: string;
  delta_used_percent: number | null;
  interval_ms: number | null;
  estimated_exhaustion_at: string | null;
  status: 'insufficient' | 'before_reset' | 'reset_first' | 'exhausted' | 'expired';
}

export function monitorIntervalMilliseconds(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const seconds = Number(value);
  return Number.isSafeInteger(seconds) && seconds >= 30 && seconds <= 1800 ? seconds * 1_000 : null;
}

export function quotaTrends(snapshots: PricingQuotaSnapshot[], now: number): QuotaTrend[] {
  const groups = new Map<string, PricingQuotaSnapshot[]>();
  for (const snapshot of snapshots) {
    const key = JSON.stringify([snapshot.account_key, snapshot.limit_id, snapshot.window_duration_ms, Date.parse(snapshot.resets_at)]);
    const group = groups.get(key) ?? [];
    group.push(snapshot);
    groups.set(key, group);
  }
  const percent = (snapshot: PricingQuotaSnapshot): number | null => {
    if (snapshot.used_percent == null) return null;
    const value = Number(snapshot.used_percent);
    return Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
  };
  return [...groups.values()].map((group): QuotaTrend => {
    group.sort((a, b) => Date.parse(b.observed_at) - Date.parse(a.observed_at));
    const latest = group[0];
    const endTime = Date.parse(latest.observed_at);
    const resetTime = Date.parse(latest.resets_at);
    const latestPercent = percent(latest);
    const trend: QuotaTrend = {
      account_key: latest.account_key, limit_id: latest.limit_id,
      window_duration_ms: latest.window_duration_ms, resets_at: latest.resets_at,
      start_snapshot_id: null, end_snapshot_id: latest.snapshot_id,
      interval_start: null, interval_end: latest.observed_at,
      delta_used_percent: null, interval_ms: null, estimated_exhaustion_at: null, status: 'insufficient',
    };
    if (resetTime <= now) return { ...trend, status: 'expired' };
    if (!Number.isFinite(resetTime) || !Number.isFinite(endTime) || latestPercent === null
      || endTime > now || endTime >= resetTime) return trend;
    if (latestPercent === 100) return { ...trend, status: 'exhausted' };
    let previousPercent = latestPercent;
    for (const earlier of group.slice(1)) {
      const earlierPercent = percent(earlier);
      const startTime = Date.parse(earlier.observed_at);
      // Missing or decreasing readings break continuity instead of becoming zeros.
      if (earlierPercent === null || earlierPercent > previousPercent || !Number.isFinite(startTime)
        || startTime < resetTime - latest.window_duration_ms) return trend;
      previousPercent = earlierPercent;
      const interval = endTime - startTime;
      const delta = Number((latestPercent - earlierPercent).toFixed(8));
      if (interval < 300_000 || delta <= 1) continue;
      const projected = endTime + (100 - latestPercent) * interval / delta;
      const projectedIso = epochMsToIso(projected);
      if (projectedIso === null) return trend;
      return {
        ...trend, start_snapshot_id: earlier.snapshot_id, interval_start: earlier.observed_at,
        delta_used_percent: delta, interval_ms: interval,
        estimated_exhaustion_at: projectedIso,
        status: projected >= resetTime ? 'reset_first' : 'before_reset',
      };
    }
    return trend;
  }).sort((a, b) => Date.parse(b.interval_end) - Date.parse(a.interval_end));
}
