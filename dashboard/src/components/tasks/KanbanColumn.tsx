import { useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { ChevronDown } from 'lucide-react';
import { TaskCard, STALE_TEXT } from './TaskCard';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import type { Task } from '@/types';

/** Which slice of the wall the stats strip asked for ("all" = no filter). */
export type WallFilter = 'all' | 'running' | 'stale' | 'blocked' | 'pending';

// Pending cards shown before the fold: the rest of the backlog is one click away.
const PENDING_VISIBLE = 8;

interface KanbanColumnProps {
  title: string;
  badgeClassName: string;
  /** The column's open tasks in wall order: pending by score, then the rest. */
  tasks: Task[];
  filter: WallFilter;
  staleDays: number;
  onTaskClick: (task: Task) => void;
}

function Fold({ open, label, onToggle, className }: {
  open: boolean; label: string; onToggle: () => void; className?: string;
}) {
  const t = useT();
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        'flex w-full items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted',
        className,
      )}
    >
      <ChevronDown className={cn('h-3 w-3 transition-transform', open && 'rotate-180')} />
      {open ? t.tasks.showLess : label}
    </button>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <p className="px-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{title}</p>
      {children}
    </div>
  );
}

/**
 * One horizon column, sectioned: running (latest activity first, stale ones folded
 * into a count), blocked and failed, then pending by score with the tail folded.
 * Every open task is either a card or inside a fold's count.
 */
export function KanbanColumn({ title, badgeClassName, tasks, filter, staleDays, onTaskClick }: KanbanColumnProps) {
  const t = useT();
  const [staleOpen, setStaleOpen] = useState(false);
  const [pendingOpen, setPendingOpen] = useState(false);

  const byActivity = (a: Task, b: Task) => (b.last_activity_at ?? '').localeCompare(a.last_activity_at ?? '');
  const active = tasks.filter((task) => task.status === 'running' && !task.stale).sort(byActivity);
  const stale = tasks.filter((task) => task.status === 'running' && task.stale)
    .sort((a, b) => (b.idle_days ?? 0) - (a.idle_days ?? 0));
  const stuck = tasks.filter((task) => task.status === 'blocked' || task.status === 'failed');
  const pending = tasks.filter((task) => task.status === 'pending');

  const show = (slice: WallFilter) => filter === 'all' || filter === slice;
  const showRunning = show('running') || filter === 'stale';
  const staleShown = staleOpen || filter === 'running' || filter === 'stale';
  const pendingShown = pendingOpen || filter === 'pending' ? pending : pending.slice(0, PENDING_VISIBLE);
  const card = (task: Task) => (
    <TaskCard key={task.id} task={task} staleDays={staleDays} onClick={() => onTaskClick(task)} />
  );

  const sections: React.ReactNode[] = [];
  if (showRunning && (active.length || stale.length)) {
    sections.push(
      <Section key="running" title={t.tasks.sectionRunning}>
        {filter !== 'stale' && active.map(card)}
        {stale.length > 0 && filter !== 'stale' && filter !== 'running' && (
          <Fold
            open={staleOpen}
            label={t.tasks.staleFolded(stale.length)}
            onToggle={() => setStaleOpen(!staleOpen)}
            className={STALE_TEXT}
          />
        )}
        {staleShown && stale.map(card)}
      </Section>,
    );
  }
  if (show('blocked') && stuck.length) {
    sections.push(<Section key="blocked" title={t.tasks.sectionBlocked}>{stuck.map(card)}</Section>);
  }
  if (show('pending') && pending.length) {
    sections.push(
      <Section key="pending" title={t.tasks.sectionPending}>
        {pendingShown.map(card)}
        {pending.length > PENDING_VISIBLE && filter !== 'pending' && (
          <Fold
            open={pendingOpen}
            label={t.tasks.pendingFolded(pending.length - PENDING_VISIBLE)}
            onToggle={() => setPendingOpen(!pendingOpen)}
          />
        )}
      </Section>,
    );
  }

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="mb-3 px-1">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium">{title}</h3>
          <Badge className={badgeClassName}>{tasks.length}</Badge>
        </div>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {t.tasks.columnBreakdown(active.length + stale.length, stuck.length, pending.length)}
        </p>
      </div>
      <div className="flex flex-col gap-4">
        {sections.length ? sections : (
          <p className="py-8 text-center text-xs text-muted-foreground">{t.tasks.noTasks}</p>
        )}
      </div>
    </div>
  );
}
