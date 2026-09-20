import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { APIResponse } from '@/types';
import type { PlanCapacityEstimate } from './planUsage';
import { accountReadOptions } from '@/lib/account-connection';

export interface PricingPlanCapacityEstimate {
  account_key: string;
  limit_id: string;
  window_duration_ms: number;
  resets_at: string;
  observed_at: string;
  used_percent: number | null;
  estimated_total_usd: string | null;
  prediction_basis?: 'cycle_anchor_missing_zero' | null;
  last_estimated_total_usd?: string | null;
  last_estimate_observed_at?: string | null;
  delta_usd: string | null;
  delta_used_percent: number | null;
  start_snapshot_id: string | null;
  end_snapshot_id: string;
  interval_start: string | null;
  status: 'estimated' | 'collecting' | 'unavailable' | 'expired';
  source: 'codex_local_logs';
  pricing_mode: 'standard_equivalent' | 'logged_tier' | null;
  catalog_version: string | null;
  catalog_sha256: string | null;
  reason_code: 'local_usage_unavailable' | 'bucket_activity_unattributed' | 'pricing_unavailable' | 'pricing_incomplete' | null;
}

export interface PricingPlanAnchorReset {
  limit_id: 'codex';
  window_duration_ms: number;
}

export interface PricingPlanAnchor {
  account_key: string;
  limit_id: 'codex';
  window_duration_ms: number;
  snapshot_id: string;
  observed_at: string;
  used_percent: number;
  resets_at: string;
  reset_at: string;
  revision: number;
}

interface AccountPlanPricingDetail {
  pricing_plan_estimates?: PricingPlanCapacityEstimate[];
  plan_estimates?: PlanCapacityEstimate[];
}

export function usePricingPlanCapacity(key: string) {
  return useQuery({
    ...accountReadOptions,
    queryKey: ['account-usage', key, 'plan'],
    enabled: Boolean(key),
    queryFn: async () => (await apiFetch<APIResponse<AccountPlanPricingDetail>>(
      '/api/account-usage/' + encodeURIComponent(key) + '?include_pricing=false',
    )).data,
  });
}

export function useResetPricingPlanAnchor(key: string) {
  const client = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: async ({ limit_id, window_duration_ms }: PricingPlanAnchorReset) =>
      (await apiFetch<APIResponse<PricingPlanAnchor>>(
        '/api/account-usage/' + encodeURIComponent(key) + '/plan-anchor/reset',
        { method: 'POST', body: JSON.stringify({ limit_id, window_duration_ms }) },
      )).data,
    onSuccess: () => client.invalidateQueries({ queryKey: ['account-usage', key, 'plan'], exact: true }),
  });
}
