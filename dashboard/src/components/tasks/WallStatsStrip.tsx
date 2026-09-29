import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import type { TaskWallDigest } from '@/types';
import type { WallFilter } from './KanbanColumn';

/** "+3" or "\u22122" for a nonzero count, a bare "0" otherwise (a signed zero reads as a change). */
function signed(count: number, sign: string): string {
  return count ? `${sign}${count}` : '0';
}

interface Cell {
  filter: WallFilter;
  label: string;
  value: number | string;
  detail: string;
  tone?: string;
  title?: string;
}

/**
 * The whole wall in one row, from the digest the Leader's briefing reads: open by
 * horizon, running, stale, blocked, pending, last 7 days. A cell is a filter for
 * the columns below; clicking the active one, or "open", clears it.
 */
export function WallStatsStrip({ digest, filter, onFilter }: {
  digest: TaskWallDigest;
  filter: WallFilter;
  onFilter: (filter: WallFilter) => void;
}) {
  const t = useT();
  const running = digest.by_status.running ?? 0;
  const blockedDays = digest.blocked_oldest_days == null ? null : Math.floor(digest.blocked_oldest_days);
  const cells: Cell[] = [
    {
      filter: 'all',
      label: t.tasks.statOpen,
      value: digest.open_total,
      detail: t.tasks.statOpenDetail(digest.by_horizon.short, digest.by_horizon.mid, digest.by_horizon.long),
    },
    {
      filter: 'running',
      label: t.tasks.statRunning,
      value: running,
      detail: t.tasks.statRunningDetail(running - digest.stale_running),
    },
    {
      filter: 'stale',
      label: t.tasks.statStale,
      value: digest.stale_running,
      detail: t.tasks.statStaleDetail(digest.stale_days),
      tone: digest.stale_running ? 'text-amber-700 dark:text-amber-400' : undefined,
      title: t.tasks.staleDefinition(digest.stale_days),
    },
    {
      filter: 'blocked',
      label: t.tasks.statBlocked,
      value: (digest.by_status.blocked ?? 0) + (digest.by_status.failed ?? 0),
      detail: t.tasks.statBlockedDetail(blockedDays),
      tone: blockedDays != null ? 'text-rose-700 dark:text-rose-400' : undefined,
    },
    {
      filter: 'pending',
      label: t.tasks.statPending,
      value: digest.by_status.pending ?? 0,
      detail: t.tasks.statPendingDetail(digest.dormant_pending, digest.dormant_days),
    },
  ];

  return (
    <div className="space-y-1.5">
      <div role="group" aria-label={t.tasks.statsLabel} className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        {cells.map((cell) => {
          const selected = cell.filter !== 'all' && filter === cell.filter;
          return (
            <button
              key={cell.filter}
              type="button"
              aria-pressed={selected}
              onClick={() => onFilter(selected || cell.filter === 'all' ? 'all' : cell.filter)}
              className={cn(
                'rounded-lg border bg-card px-3 py-2 text-left transition-colors hover:bg-muted/60',
                selected && 'border-primary ring-1 ring-primary',
              )}
            >
              <p className="text-xs text-muted-foreground">{cell.label}</p>
              <p className="text-xl font-semibold tabular-nums">{cell.value}</p>
              <p className={cn('truncate text-xs text-muted-foreground', cell.tone)} title={cell.title}>
                {cell.detail}
              </p>
            </button>
          );
        })}
        <div className="rounded-lg border bg-card px-3 py-2">
          <p className="text-xs text-muted-foreground">{t.tasks.statWeek}</p>
          <p className="text-xl font-semibold tabular-nums">
            {signed(digest.created_7d, '+')} <span className="text-muted-foreground">/</span>{' '}
            {signed(digest.closed_7d, '\u2212')}
          </p>
          <p className="truncate text-xs text-muted-foreground">
            {t.tasks.statWeekDetail(digest.created_7d, digest.closed_7d)}
          </p>
        </div>
      </div>
      {filter !== 'all' && <p className="px-1 text-xs text-muted-foreground">{t.tasks.statFilterOn}</p>}
    </div>
  );
}
