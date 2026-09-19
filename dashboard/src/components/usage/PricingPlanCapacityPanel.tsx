import { useRef, useState } from 'react';
import { usePricingPlanCapacity, useResetPricingPlanAnchor } from '@/api/pricingPlanUsage';
import type { PricingPlanCapacityEstimate } from '@/api/pricingPlanUsage';
import { Button } from '@/components/ui/button';
import { useT } from '@/i18n';
import { parseServerTime } from '@/lib/datetime';

const LIMIT_DISPLAY_NAMES = new Map<string, string>([
  ['codex', 'Codex'],
  ['codex_bengalfox', 'GPT-5.3-Codex-Spark'],
]);

function formatPlanUsd(value: string | null | undefined, allowZero = false): string | null {
  if (typeof value !== 'string' || value.length > 1000) return null;
  const match = /^(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(value);
  if (!match) return null;
  const exponent = Number(match[3] ?? 0);
  if (!Number.isSafeInteger(exponent)) return null;
  const digits = match[1] + (match[2] ?? '');
  const first = digits.search(/[1-9]/);
  if (first < 0) return allowZero ? '$0.00' : null;
  const point = match[1].length + exponent;
  if (point - first <= -2) return '< $0.01';
  if (point > 100) return null;
  const boundary = point + 2;
  let cents = BigInt(digits.padEnd(boundary, '0').slice(0, boundary));
  if ((digits[boundary] ?? '0') >= '5') cents += 1n;
  const whole = (cents / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return '$' + whole + '.' + (cents % 100n).toString().padStart(2, '0');
}

function canResetPlanAnchor(estimate: PricingPlanCapacityEstimate): boolean {
  const observed = parseServerTime(estimate.observed_at)?.getTime();
  const resets = parseServerTime(estimate.resets_at)?.getTime();
  const now = Date.now();
  return estimate.limit_id === 'codex' && estimate.source === 'codex_local_logs'
    && estimate.status !== 'expired' && Number.isSafeInteger(estimate.window_duration_ms)
    && estimate.window_duration_ms > 0 && Number.isInteger(estimate.used_percent)
    && estimate.used_percent !== null && estimate.used_percent >= 0 && estimate.used_percent <= 100
    && typeof estimate.end_snapshot_id === 'string' && Boolean(estimate.end_snapshot_id.trim())
    && observed !== undefined && resets !== undefined && resets > now && observed <= now
    && observed < resets && observed >= resets - estimate.window_duration_ms;
}

function PricingPlanAnchorResetButton({ estimate }: { estimate: PricingPlanCapacityEstimate }) {
  const t = useT().accountUsage;
  const reset = useResetPricingPlanAnchor(estimate.account_key);
  const pending = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const resetAnchor = async () => {
    if (pending.current || !canResetPlanAnchor(estimate)) return;
    pending.current = true;
    setBusy(true);
    setError('');
    try {
      await reset.mutateAsync({ limit_id: 'codex', window_duration_ms: estimate.window_duration_ms });
    } catch (error) {
      setError(error instanceof Error ? error.message : t.planAnchorResetError);
    } finally {
      pending.current = false;
      setBusy(false);
    }
  };
  if (!canResetPlanAnchor(estimate)) return null;
  return (
    <div className="mt-4">
      <Button type="button" variant="outline" size="sm" disabled={busy || reset.isPending}
        aria-busy={busy || reset.isPending} onClick={resetAnchor}>
        {busy || reset.isPending ? t.planAnchorResetting : t.planAnchorReset}
      </Button>
      {error && <p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
    </div>
  );
}

export function PricingPlanCapacityPanel({ accountKey }: { accountKey: string }) {
  const t = useT().accountUsage;
  const query = usePricingPlanCapacity(accountKey);
  const windowKey = (item: { account_key: string; limit_id: string; window_duration_ms: number; resets_at: string }) =>
    JSON.stringify([item.account_key, item.limit_id, item.window_duration_ms, item.resets_at]);
  const priced = query.data?.pricing_plan_estimates ?? [];
  const pricedKeys = new Set(priced.map(windowKey));
  const legacy = (query.data?.plan_estimates ?? []).filter((item) => !pricedKeys.has(windowKey(item)));
  const estimates = [...priced, ...legacy].filter((item) => item.account_key === accountKey);
  const statuses = { estimated: t.planUnavailable, collecting: t.planCollecting,
    unavailable: t.planUnavailable, expired: t.planExpired };
  const windowLabel = (duration: number) => duration === 604_800_000 ? t.planWeek
    : duration % 86_400_000 === 0 ? t.planDays(duration / 86_400_000)
      : duration % 3_600_000 === 0 ? t.planHours(duration / 3_600_000) : t.planMinutes(duration / 60_000);

  return (
    <section aria-label={t.planDollarCapacity} className="space-y-3">
      {query.isLoading && <p role="status" className="text-sm text-muted-foreground">{t.planCollecting}</p>}
      {query.isError && <p role="alert" className="rounded-lg border border-destructive/40 p-3 text-sm">{query.error.message}</p>}
      {!query.isLoading && !query.isError && estimates.length === 0 && <p className="rounded-lg border p-5 text-sm text-muted-foreground">{t.planUnavailable}</p>}
      <div className="grid gap-4 lg:grid-cols-2">{estimates.map((estimate) => {
        const pricing = priced.find((item) => item === estimate) ?? null;
        const cyclePrediction = pricing?.prediction_basis === 'cycle_anchor_missing_zero';
        const amount = formatPlanUsd(pricing?.estimated_total_usd, cyclePrediction);
        const standard = pricing?.pricing_mode === 'standard_equivalent';
        const loggedTier = pricing?.pricing_mode === 'logged_tier';
        const metadata = (standard || loggedTier) && typeof pricing?.catalog_version === 'string' && Boolean(pricing.catalog_version.trim())
          && typeof pricing?.catalog_sha256 === 'string' && /^[a-f0-9]{64}$/.test(pricing.catalog_sha256);
        const noMetadata = pricing?.pricing_mode === null && pricing?.catalog_version === null
          && pricing?.catalog_sha256 === null;
        const predictionMetadata = cyclePrediction ? metadata || noMetadata : metadata;
        const predictionReason = pricing?.reason_code == null || (cyclePrediction
          && (pricing.reason_code === 'pricing_incomplete' || pricing.reason_code === 'pricing_unavailable'));
        const available = pricing?.source === 'codex_local_logs' && pricing.limit_id === 'codex'
          && pricing.status === 'estimated' && predictionReason && predictionMetadata
          && amount !== null && formatPlanUsd(pricing.delta_usd, cyclePrediction) !== null
          && Number.isInteger(pricing.delta_used_percent) && pricing.delta_used_percent !== null
          && pricing.delta_used_percent > 0 && pricing.delta_used_percent <= 100
          && Number.isInteger(pricing.used_percent) && pricing.used_percent !== null
          && pricing.used_percent >= 0 && pricing.used_percent <= 100
          && typeof pricing.start_snapshot_id === 'string' && Boolean(pricing.start_snapshot_id)
          && typeof pricing.interval_start === 'string' && Boolean(pricing.interval_start);
        const lastAmount = formatPlanUsd(pricing?.last_estimated_total_usd ?? null, cyclePrediction);
        const lastObservedAt = pricing?.last_estimate_observed_at ?? null;
        const lastObservation = typeof lastObservedAt === 'string'
          && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(lastObservedAt)
          ? parseServerTime(lastObservedAt) : null;
        const latestObservation = parseServerTime(pricing?.observed_at);
        const lastAvailable = pricing?.source === 'codex_local_logs' && pricing.limit_id === 'codex'
          && pricing.status !== 'expired' && lastAmount !== null && lastObservation !== null
          && latestObservation !== null && lastObservation.getTime() <= latestObservation.getTime();
        const displayedAmount = available ? amount : lastAvailable ? lastAmount : null;
        const unavailableLabel = estimate.reason_code === 'local_usage_unavailable' ? t.planLocalUsageUnavailable
          : estimate.reason_code === 'activity_coverage_unknown' ? t.planAlignedUsageUnavailable
            : pricing ? statuses[pricing.status] : estimate.status === 'expired' ? t.planExpired : t.planUnavailable;
        const limitName = LIMIT_DISPLAY_NAMES.get(estimate.limit_id) ?? estimate.limit_id;
        return (
          <article key={estimate.account_key + '-' + estimate.limit_id + '-' + estimate.window_duration_ms + '-' + estimate.resets_at}
            className="rounded-xl border bg-card p-5" aria-label={limitName + ' ' + windowLabel(estimate.window_duration_ms)}>
            <header className="mb-5 flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-semibold">{windowLabel(estimate.window_duration_ms)}</h2>
              <span className="text-xs text-muted-foreground">{limitName}
                {estimate.source === 'codex_local_logs' && <span className="ml-2">{t.planLocalSampleEstimate}</span>}
              </span>
            </header>
            <dl className="grid grid-cols-2 gap-4">
              <div><dt className="text-sm text-muted-foreground">{t.planDollarCapacity}</dt>
                <dd className="mt-2 break-words tabular-nums" data-dimension="money_usd">
                  <span className={displayedAmount !== null ? 'text-3xl font-semibold tracking-tight' : 'text-lg font-medium'}>{displayedAmount ?? unavailableLabel}</span>
                  {(standard || loggedTier || lastAvailable || (cyclePrediction && available))
                    && <p className="mt-1 text-xs text-muted-foreground">{
                      loggedTier ? t.planLoggedTier : t.planStandardEquivalent
                    }</p>}
                </dd>
              </div>
              <div><dt className="text-sm text-muted-foreground">{t.planUsed}</dt>
                <dd className="mt-2 text-3xl font-semibold tracking-tight tabular-nums">
                  {estimate.status === 'expired' ? <span className="text-lg font-medium">{t.planExpired}</span>
                    : estimate.used_percent === null ? <span className="text-lg font-medium">{t.planUnavailable}</span> : estimate.used_percent + '%'}
                </dd>
              </div>
            </dl>
            {pricing && <PricingPlanAnchorResetButton estimate={pricing} />}
          </article>
        );
      })}</div>
    </section>
  );
}
