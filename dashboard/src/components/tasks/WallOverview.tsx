import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { RelativeTime } from '@/components/shared/RelativeTime';
import { Activity, ChevronDown, Flag, OctagonAlert } from 'lucide-react';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { activityLabel, priorityConfig, statusConfig, BLOCKED_TEXT, STALE_TEXT } from './TaskCard';
import type { DigestItem, TaskWallDigest } from '@/types';

const ROWS = 5;

function Block({ icon: Icon, title, hint, children }: {
  icon: React.ElementType; title: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <Card size="sm" className="gap-2">
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5 text-sm" title={hint}>
          <Icon className="h-4 w-4 text-muted-foreground" />
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-0.5">{children}</CardContent>
    </Card>
  );
}

function Row({ item, onOpen, lead, trail }: {
  item: DigestItem; onOpen: (id: string) => void; lead?: React.ReactNode; trail?: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={() => onOpen(item.id)}
      className="flex w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-1 text-left text-sm hover:bg-muted"
      title={item.title}
    >
      {lead}
      <span className="min-w-0 flex-1 truncate">{item.title}</span>
      {trail}
    </button>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="px-1.5 py-2 text-xs text-muted-foreground">{text}</p>;
}

/**
 * Recent activity, top 5 and stuck tasks: the same lists the Leader's briefing
 * shows, from the same digest. Rows open the task's detail.
 */
export function WallOverview({ digest, onOpen }: { digest: TaskWallDigest; onOpen: (id: string) => void }) {
  const t = useT();
  const [stuckOpen, setStuckOpen] = useState(false);
  const days = (n: number | null | undefined) => Math.floor(n ?? 0);
  const stuck = stuckOpen ? digest.stuck : digest.stuck.slice(0, ROWS);

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
      <Block icon={Activity} title={t.tasks.overviewRecent}>
        {digest.recent.length === 0 && <Empty text={t.tasks.overviewRecentEmpty} />}
        {digest.recent.map((item) => {
          const status = statusConfig(item.status, t);
          return (
            <Row
              key={item.id}
              item={item}
              onOpen={onOpen}
              lead={
                <span className="w-24 shrink-0 truncate text-xs text-muted-foreground">
                  <RelativeTime date={item.activity_at} /> · {activityLabel(item.activity_kind, item.activity_memo_type, t)}
                </span>
              }
              trail={<Badge className={cn(status.className, 'h-4 px-1.5 text-[10px]')}>{status.label}</Badge>}
            />
          );
        })}
      </Block>

      <Block icon={Flag} title={t.tasks.overviewTop} hint={t.tasks.overviewTopHint}>
        {digest.top.length === 0 && <Empty text={t.tasks.overviewTopEmpty} />}
        {digest.top.map((item) => {
          const priority = priorityConfig(item.priority, t);
          return (
            <Row
              key={item.id}
              item={item}
              onOpen={onOpen}
              lead={<Badge className={cn(priority.className, 'h-4 px-1.5 text-[10px]')}>{priority.label}</Badge>}
              trail={<span className="shrink-0 text-xs text-muted-foreground">{t.tasks.wallDays(days(item.wall_days))}</span>}
            />
          );
        })}
      </Block>

      <Block icon={OctagonAlert} title={t.tasks.overviewStuck(digest.stuck.length)} hint={t.tasks.staleDefinition(digest.stale_days)}>
        {digest.stuck.length === 0 && <Empty text={t.tasks.overviewStuckEmpty} />}
        {stuck.map((item) => (
          <Row
            key={item.id}
            item={item}
            onOpen={onOpen}
            lead={
              <span className={cn('w-20 shrink-0 text-xs font-medium',
                item.status === 'running' ? STALE_TEXT : BLOCKED_TEXT)}>
                {item.status === 'blocked'
                  ? t.tasks.blockedDays(days(item.blocked_days))
                  : item.status === 'running'
                    ? t.tasks.staleDays(days(item.idle_days))
                    : statusConfig(item.status, t).label}
              </span>
            }
          />
        ))}
        {digest.stuck.length > ROWS && (
          <button
            type="button"
            onClick={() => setStuckOpen(!stuckOpen)}
            className="flex w-full items-center gap-1 rounded-md px-1.5 py-1 text-xs text-muted-foreground hover:bg-muted"
          >
            <ChevronDown className={cn('h-3 w-3 transition-transform', stuckOpen && 'rotate-180')} />
            {stuckOpen ? t.tasks.showLess : t.tasks.showMore(digest.stuck.length - ROWS)}
          </button>
        )}
      </Block>
    </div>
  );
}
