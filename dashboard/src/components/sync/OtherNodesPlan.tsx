import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '@/api/client';
import { NodeBadge } from '@/components/sync/NodeBadge';
import { useT } from '@/i18n';
import { formatDateTimeSystemLocale } from '@/lib/datetime';

export interface OtherNodePlan {
  origin_node: string;
  account_key: string;
  limit_id: string;
  window_duration_ms: number;
  used_percent: number;
  resets_at: string | null;
  observed_at: string;
}

/** Other machines' latest plan usage observation (multi-host sync, on the center). Each machine
 * folds its own plan cycles; here each is shown as last observed, with its machine. */
export function OtherNodesPlan() {
  const t = useT().sync;
  const { data } = useQuery({
    queryKey: ['account-usage', 'other-nodes'],
    queryFn: () => apiFetch<{ data: { items: OtherNodePlan[] } }>('/api/account-usage/other-nodes'),
  });
  const items = data?.data.items ?? [];
  if (items.length === 0) return null;
  return (
    <section className="space-y-2 rounded-lg border p-4" aria-label={t.otherNodesPlan}>
      <h2 className="text-lg font-semibold">{t.otherNodesPlan}</h2>
      <p className="text-sm text-muted-foreground">{t.otherNodesPlanHint}</p>
      <ul className="divide-y text-sm">
        {items.map((item) => (
          <li key={`${item.origin_node}:${item.account_key}:${item.limit_id}:${item.window_duration_ms}`}
            className="flex flex-wrap items-center gap-3 py-2">
            <NodeBadge origin={item.origin_node} />
            <span className="font-mono text-xs">…{item.account_key.slice(-8)} · {item.limit_id}</span>
            <span className="font-medium">{t.usedPercent(item.used_percent)}</span>
            <span className="text-xs text-muted-foreground">{t.lastKnown(formatDateTimeSystemLocale(item.observed_at))}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
