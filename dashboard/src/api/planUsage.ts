import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { APIResponse } from '@/types';
import type { AccountUsageDetail } from './accountUsage';

export interface PlanCapacityEstimate {
  account_key: string;
  limit_id: string;
  window_duration_ms: number;
  resets_at: string;
  observed_at: string;
  used_percent: number | null;
  estimated_total_tokens: number | null;
  delta_tokens: number | null;
  delta_used_percent: number | null;
  start_snapshot_id: string | null;
  end_snapshot_id: string;
  interval_start: string | null;
  status: 'estimated' | 'collecting' | 'unavailable' | 'expired';
  reason_code?: 'activity_coverage_unknown' | 'bucket_activity_unattributed' | 'local_usage_unavailable' | null;
  source: 'codex_account_activity' | 'codex_local_logs';
}

interface PlanAccountDetail extends AccountUsageDetail {
  plan_estimates: PlanCapacityEstimate[];
}

export function usePlanCapacity(key: string) {
  return useQuery({
    queryKey: ['account-usage', key, 'plan'],
    enabled: Boolean(key),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
    queryFn: async () => (await apiFetch<APIResponse<PlanAccountDetail>>(
      '/api/account-usage/' + encodeURIComponent(key) + '?include_pricing=false',
    )).data,
    select: (data: PlanAccountDetail) => data.plan_estimates ?? [],
  });
}
