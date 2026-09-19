import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { APIResponse } from '@/types';

export interface PricingAccount {
  account_key: string;
  label: string;
  created_at: string;
}

export interface PricingQuotaSnapshot {
  snapshot_id: string;
  account_key: string;
  limit_id: string;
  used_percent: string | null;
  window_duration_ms: number;
  resets_at: string;
  observed_at: string;
  source: 'codex_app_server' | 'user_import';
}

export interface PricingRequestLine {
  request_id: string;
  model: string;
  service_tier: 'standard' | 'default' | 'fast' | 'priority' | 'flex' | 'batch';
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  cache_write_input_tokens: number;
}

export interface PricingUsageEntry {
  occurred_at: string;
  request: PricingRequestLine;
}

export interface PricingUsageBatch {
  batch_id: string;
  account_key: string;
  start_snapshot_id: string;
  end_snapshot_id: string;
  coverage: 'local_only' | 'account_complete';
  coverage_statement: string;
  coverage_confirmed_at: string | null;
  entries: PricingUsageEntry[];
}

export interface PricingRates {
  input: string;
  cached_input: string | null;
  cache_write: string | null;
  output: string;
}

export interface PricingRateRecord {
  model: string;
  tier: 'standard' | 'fast' | 'flex' | 'batch';
  min_input_tokens: number;
  max_input_tokens: number | null;
  rates: PricingRates;
  source_url: string;
  verified_at: string;
  effective_from: string | null;
  effective_until: string | null;
  notes: string;
}

export interface PricingQuoteItem {
  request_id: string;
  model: string;
  canonical_model: string | null;
  service_tier: PricingRequestLine['service_tier'];
  status: 'priced' | 'unpriced';
  reason: string | null;
  amount_usd: string | null;
  rate_record: PricingRateRecord | null;
}

export interface PricingQuoteResponse {
  basis: 'api_equivalent_at_catalog_version';
  currency: 'USD';
  catalog_version: string;
  catalog_sha256: string;
  verified_at: string;
  request_count: number;
  priced_request_count: number;
  unpriced_request_count: number;
  complete: boolean;
  total_usd: string | null;
  priced_subtotal_usd: string;
  items: PricingQuoteItem[];
  missing_models: string[];
}

export interface PricingAccountEstimate {
  batch_id: string;
  account_key: string;
  start_snapshot_id: string;
  end_snapshot_id: string;
  coverage: PricingUsageBatch['coverage'];
  coverage_statement: string;
  coverage_confirmed_at: string | null;
  interval_start: string;
  interval_end: string;
  delta_used_percent: string | null;
  quote: PricingQuoteResponse;
  estimated_full_week_usd: string | null;
  status: 'sample_only' | 'conditional' | 'unavailable';
  reason: string | null;
}

export interface AccountUsageDetail {
  account: PricingAccount;
  snapshots: PricingQuotaSnapshot[];
  estimates: PricingAccountEstimate[];
}

export interface PricingMonitorSettings {
  enabled: boolean;
  interval_ms: number;
}

export interface PricingMonitorState {
  account_key: string;
  settings: PricingMonitorSettings;
  revision: number;
  status: 'disabled' | 'waiting' | 'sampling' | 'error' | 'paused_account_changed';
  runtime_running: boolean;
  last_started_at: string | null;
  last_finished_at: string | null;
  next_run_at: string | null;
  last_error: string | null;
}

const ROOT = '/api/account-usage';
const accountPath = (key: string) => `${ROOT}/${encodeURIComponent(key)}`;
const readOptions = { refetchOnWindowFocus: false, refetchOnReconnect: false, retry: false } as const;

export function usePricingAccounts() {
  return useQuery({
    ...readOptions,
    queryKey: ['account-usage'],
    queryFn: async () => (await apiFetch<APIResponse<{ accounts: PricingAccount[] }>>(ROOT)).data,
  });
}

export function usePricingAccount(key: string, includePricing = true) {
  return useQuery({
    ...readOptions,
    queryKey: includePricing ? ['account-usage', key] : ['account-usage', key, 'plan'],
    enabled: Boolean(key),
    queryFn: async () => (await apiFetch<APIResponse<AccountUsageDetail>>(
      accountPath(key) + (includePricing ? '' : '?include_pricing=false'),
    )).data,
  });
}

export function useCapturePricingAccount() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: async () => (await apiFetch<APIResponse<Pick<AccountUsageDetail, 'account' | 'snapshots'>>>(
      `${ROOT}/capture`, { method: 'POST', body: '{}' },
    )).data,
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['account-usage'] }); },
  });
}

export function useImportPricingBatch() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: async (batch: PricingUsageBatch) => (await apiFetch<APIResponse<PricingAccountEstimate>>(
      `${accountPath(batch.account_key)}/batches`, { method: 'POST', body: JSON.stringify(batch) },
    )).data,
    onSuccess: (_result, batch) => {
      void client.invalidateQueries({ queryKey: ['account-usage', batch.account_key] });
    },
  });
}

export function useLabelPricingAccount() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: async ({ key, label }: { key: string; label: string }) => (
      await apiFetch<APIResponse<PricingAccount>>(`${accountPath(key)}/label`, {
        method: 'PATCH', body: JSON.stringify({ label }),
      })
    ).data,
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['account-usage'] }); },
  });
}

export function usePricingMonitor(key: string) {
  const client = useQueryClient();
  return useQuery({
    ...readOptions,
    queryKey: ['account-usage', key, 'monitor'],
    enabled: Boolean(key),
    refetchInterval: 10_000,
    queryFn: async () => {
      const previous = client.getQueryData<PricingMonitorState>(['account-usage', key, 'monitor']);
      const result = (await apiFetch<APIResponse<PricingMonitorState>>(`${accountPath(key)}/monitor`)).data;
      if (result.last_finished_at && result.last_finished_at !== previous?.last_finished_at) {
        void client.invalidateQueries({ queryKey: ['account-usage', key], exact: true });
        void client.invalidateQueries({ queryKey: ['account-usage', key, 'plan'], exact: true });
      }
      return result;
    },
  });
}

export function useUpdatePricingMonitor() {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: async ({ key, settings }: {
      key: string;
      settings: { enabled: true; interval_ms: number } | { enabled: false; interval_ms?: number };
    }) => (
      await apiFetch<APIResponse<PricingMonitorState>>(`${accountPath(key)}/monitor`, {
        method: 'PUT', body: JSON.stringify(settings),
      })
    ).data,
    onSuccess: (result, { key }) => {
      const previous = client.getQueryData<PricingMonitorState>(['account-usage', key, 'monitor']);
      if (result.last_finished_at && result.last_finished_at !== previous?.last_finished_at) {
        void client.invalidateQueries({ queryKey: ['account-usage', key], exact: true });
        void client.invalidateQueries({ queryKey: ['account-usage', key, 'plan'], exact: true });
      }
      client.setQueryData(['account-usage', key, 'monitor'], result);
      void client.invalidateQueries({ queryKey: ['account-usage', key, 'monitor'], exact: true });
    },
  });
}
