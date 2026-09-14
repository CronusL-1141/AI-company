import { usePlanCapacity } from '@/api/planUsage';
import { useT } from '@/i18n';

function formatPlanTokens(value: number | null): string | null {
  if (value === null || !Number.isSafeInteger(value) || value < 0) return null;
  return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
}

export function PlanCapacityPanel({ accountKey }: { accountKey: string }) {
  const t = useT().accountUsage;
  const query = usePlanCapacity(accountKey);
  const estimates = query.data?.filter((item) => item.account_key === accountKey) ?? [];
  const statuses = {
    estimated: t.planUnavailable, collecting: t.planCollecting,
    unavailable: t.planUnavailable, expired: t.planExpired,
  };
  const windowLabel = (duration: number) => duration === 604_800_000 ? t.planWeek
    : duration % 86_400_000 === 0 ? t.planDays(duration / 86_400_000)
      : duration % 3_600_000 === 0 ? t.planHours(duration / 3_600_000)
        : t.planMinutes(duration / 60_000);

  return (
    <section aria-label={t.planCapacity} className="space-y-3">
      {query.isLoading && <p role="status" className="text-sm text-muted-foreground">{t.planCollecting}</p>}
      {query.isError && <p role="alert" className="rounded-lg border border-destructive/40 p-3 text-sm">{query.error.message}</p>}
      {!query.isLoading && !query.isError && estimates.length === 0 && <p className="rounded-lg border p-5 text-sm text-muted-foreground">{t.planUnavailable}</p>}
      <div className="grid gap-4 lg:grid-cols-2">{estimates.map((estimate) => {
        const amount = formatPlanTokens(estimate.estimated_total_tokens);
        const localSample = estimate.source === 'codex_local_logs';
        const available = localSample && estimate.status === 'estimated' && amount !== null && estimate.reason_code == null;
        const unavailableLabel = estimate.reason_code === 'activity_coverage_unknown' ? t.planAlignedUsageUnavailable
          : estimate.reason_code === 'local_usage_unavailable' ? t.planLocalUsageUnavailable
            : estimate.reason_code === 'bucket_activity_unattributed' || (!localSample && estimate.status !== 'expired')
              ? t.planUnavailable : statuses[estimate.status];
        return (
          <article key={estimate.limit_id + '-' + estimate.window_duration_ms + '-' + estimate.resets_at}
            className="rounded-xl border bg-card p-5" aria-label={estimate.limit_id + ' ' + windowLabel(estimate.window_duration_ms)}>
            <header className="mb-5 flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-semibold">{windowLabel(estimate.window_duration_ms)}</h2>
              <span className="text-xs text-muted-foreground">{estimate.limit_id}
                {localSample && <span className="ml-2">{t.planLocalSampleEstimate}</span>}
              </span>
            </header>
            <dl className="grid grid-cols-2 gap-4">
              <div><dt className="text-sm text-muted-foreground">{t.planCapacity}</dt>
                <dd className="mt-2 flex flex-wrap items-baseline gap-x-1.5 tabular-nums" data-metric="native_activity">
                  <span className={available ? 'text-3xl font-semibold tracking-tight' : 'text-lg font-medium'}>
                    {available ? amount : unavailableLabel}
                  </span>
                  {available && <span className="text-xs text-muted-foreground">Token</span>}
                </dd>
              </div>
              <div><dt className="text-sm text-muted-foreground">{t.planUsed}</dt>
                <dd className="mt-2 text-3xl font-semibold tracking-tight tabular-nums">
                  {estimate.status === 'expired' ? <span className="text-lg font-medium">{t.planExpired}</span>
                    : estimate.used_percent === null ? <span className="text-lg font-medium">{t.planUnavailable}</span>
                      : estimate.used_percent + '%'}
                </dd>
              </div>
            </dl>
          </article>
        );
      })}</div>
    </section>
  );
}
