import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { RelativeTime } from '@/components/shared/RelativeTime';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import type { Task, TaskActivityKind } from '@/types';

export function statusConfig(status: string, t: ReturnType<typeof useT>) {
  const map: Record<string, { label: string; className: string }> = {
    pending: { label: t.taskCard.statusPending, className: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400' },
    running: { label: t.taskCard.statusRunning, className: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400' },
    blocked: { label: t.taskCard.statusBlocked, className: 'bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-400' },
    completed: { label: t.taskCard.statusCompleted, className: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400' },
    failed: { label: t.taskCard.statusFailed, className: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400' },
  };
  return map[status] ?? { label: status, className: '' };
}

export function priorityConfig(priority: string, t: ReturnType<typeof useT>) {
  const map: Record<string, { label: string; className: string }> = {
    critical: { label: t.taskCard.priorityCritical, className: 'bg-red-500 text-white' },
    high: { label: t.taskCard.priorityHigh, className: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-400' },
    medium: { label: t.taskCard.priorityMedium, className: 'bg-sky-100 text-sky-800 dark:bg-sky-900/30 dark:text-sky-400' },
    low: { label: t.taskCard.priorityLow, className: 'bg-gray-100 text-gray-600 dark:bg-gray-800/50 dark:text-gray-400' },
  };
  return map[priority] ?? { label: priority, className: '' };
}

/** "decision", "closed", ...: what the task's latest work action was. */
export function activityLabel(
  kind: TaskActivityKind | undefined,
  memoType: string | undefined,
  t: ReturnType<typeof useT>,
): string {
  if (kind === 'memo') {
    const memo: Record<string, string> = {
      progress: t.tasks.memoProgress,
      decision: t.tasks.memoDecision,
      issue: t.tasks.memoIssue,
      summary: t.tasks.memoSummary,
    };
    return memo[memoType ?? ''] ?? t.tasks.memoOther;
  }
  const map: Record<string, string> = {
    created: t.tasks.kindCreated,
    started: t.tasks.kindStarted,
    blocked: t.tasks.kindBlocked,
    reopened: t.tasks.kindReopened,
    failed: t.tasks.kindFailed,
    closed: t.tasks.kindClosed,
    subtask: t.tasks.kindSubtask,
  };
  return map[kind ?? 'created'] ?? '';
}

/** Amber for stale running work, rose for blocked: the two ways a task is stuck. */
export const STALE_TEXT = 'text-amber-700 dark:text-amber-400';
export const BLOCKED_TEXT = 'text-rose-700 dark:text-rose-400';

/**
 * One wall card: title (two lines), priority, and one line of "status · latest
 * activity · who". Description and tags live in the detail dialog.
 */
export function TaskCard({ task, onClick, staleDays }: { task: Task; onClick: () => void; staleDays: number }) {
  const t = useT();
  const sCfg = statusConfig(task.status, t);
  const pCfg = priorityConfig(task.priority, t);
  const stale = task.stale === true;
  const days = (n: number | null | undefined) => Math.floor(n ?? 0);

  return (
    <Card
      size="sm"
      className={cn(
        'cursor-pointer gap-1.5 py-2.5 transition-shadow hover:shadow-md',
        task.status === 'running' && !stale && 'border-l-4 border-l-blue-500',
        stale && 'bg-muted/60 border-l-4 border-l-amber-400',
        task.status === 'blocked' && 'border-l-4 border-l-rose-400',
      )}
      onClick={onClick}
    >
      <CardHeader className="px-3">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-sm font-medium leading-snug line-clamp-2" title={task.title}>
            {task.title}
          </CardTitle>
          <Badge className={pCfg.className}>{pCfg.label}</Badge>
        </div>
      </CardHeader>
      <CardContent className="px-3">
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground min-w-0">
          <Badge className={cn(sCfg.className, 'h-4 px-1.5 text-[10px]')}>{sCfg.label}</Badge>
          {task.status === 'pending' ? (
            <span className="truncate">{t.tasks.wallDays(days(task.wall_days))}</span>
          ) : (
            <span className="truncate min-w-0">
              {task.last_activity_at && (
                <>
                  {activityLabel(task.last_activity_kind, task.last_activity_memo_type, t)}{' '}
                  <RelativeTime date={task.last_activity_at} />
                </>
              )}
              {task.last_activity_by && <> · {task.last_activity_by}</>}
            </span>
          )}
          {stale && (
            <span className={cn('ml-auto shrink-0 font-medium', STALE_TEXT)} title={t.tasks.staleDefinition(staleDays)}>
              {t.tasks.staleDays(days(task.idle_days))}
            </span>
          )}
          {task.status === 'blocked' && task.blocked_days != null && (
            <span className={cn('ml-auto shrink-0 font-medium', BLOCKED_TEXT)}>
              {t.tasks.blockedDays(days(task.blocked_days))}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
