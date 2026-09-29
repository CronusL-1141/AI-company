import { useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { CheckCircle2, ChevronDown } from 'lucide-react';
import { completedTasksKey, useCompletedTasks } from '@/api/tasks';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { formatDate, formatTime, parseServerTime } from '@/lib/datetime';
import { priorityConfig } from './TaskCard';
import type { CompletedTaskRow } from '@/types';

/** Local calendar day of a completion, as a sortable key. */
function dayKey(row: CompletedTaskRow): string {
  const d = parseServerTime(row.completed_at ?? row.created_at);
  if (!d) return '';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/**
 * Completed tasks, collapsed by default: the header counts come from the digest,
 * and short rows load 50 at a time only once the section is opened (the full rows
 * of every completed task made the page's first load 1.48 MB).
 */
export function CompletedSection({ projectId, total, lastWeek, onOpen }: {
  projectId: string;
  total: number;
  lastWeek: number;
  onOpen: (id: string) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const query = useCompletedTasks(projectId, open);
  // Task events refetch the wall only; the list follows when its count moves.
  const queryClient = useQueryClient();
  const seenTotal = useRef(total);
  useEffect(() => {
    if (seenTotal.current === total) return;
    seenTotal.current = total;
    void queryClient.invalidateQueries({ queryKey: completedTasksKey(projectId) });
  }, [total, projectId, queryClient]);
  const rows = useMemo(() => query.data?.pages.flatMap((page) => page.completed) ?? [], [query.data]);
  const days = useMemo(() => {
    const groups = new Map<string, CompletedTaskRow[]>();
    for (const row of rows) {
      const key = dayKey(row);
      groups.set(key, [...(groups.get(key) ?? []), row]);
    }
    return [...groups.entries()];
  }, [rows]);

  return (
    <div>
      <Button
        variant="ghost"
        className="h-auto w-full justify-between px-3 py-2"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span className="flex items-center gap-2 text-sm font-medium">
          <CheckCircle2 className="h-4 w-4 text-green-600" />
          {t.tasks.completedHeader(total, lastWeek)}
        </span>
        <ChevronDown className={cn('h-4 w-4 transition-transform', open && 'rotate-180')} />
      </Button>
      {open && (
        <div className="mt-2 space-y-3 px-1">
          {query.isLoading && <Skeleton className="h-24" />}
          {!query.isLoading && rows.length === 0 && (
            <p className="px-2 text-xs text-muted-foreground">{t.tasks.completedEmpty}</p>
          )}
          {days.map(([day, dayRows]) => (
            <div key={day}>
              <p className="px-2 pb-1 text-xs font-medium text-muted-foreground">
                {formatDate(dayRows[0].completed_at ?? dayRows[0].created_at, { month: '2-digit', day: '2-digit', weekday: 'short' })}
                {' · '}{dayRows.length}
              </p>
              <div className="grid grid-cols-1 gap-x-4 md:grid-cols-2 xl:grid-cols-3">
                {dayRows.map((row) => {
                  const priority = priorityConfig(row.priority, t);
                  return (
                    <button
                      key={row.id}
                      type="button"
                      onClick={() => onOpen(row.id)}
                      className="flex min-w-0 items-center gap-2 rounded-md px-2 py-1 text-left text-sm hover:bg-muted"
                      title={row.title}
                    >
                      <span className="w-11 shrink-0 text-xs tabular-nums text-muted-foreground">
                        {formatTime(row.completed_at, { hour: '2-digit', minute: '2-digit', hour12: false })}
                      </span>
                      <span className="min-w-0 flex-1 truncate">{row.title}</span>
                      <Badge className={cn(priority.className, 'h-4 px-1.5 text-[10px]')}>{priority.label}</Badge>
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
          {query.hasNextPage && (
            <Button variant="outline" size="sm" disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
              {t.tasks.completedLoadMore}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
