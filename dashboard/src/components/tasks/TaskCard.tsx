import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { RelativeTime } from '@/components/shared/RelativeTime';
import { useT } from '@/i18n';
import { Users, User } from 'lucide-react';
import type { Task } from '@/types';

export function statusConfig(status: string, t: ReturnType<typeof useT>) {
  const map: Record<string, { label: string; className: string }> = {
    pending: { label: t.taskCard.statusPending, className: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400' },
    running: { label: t.taskCard.statusRunning, className: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400' },
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

export function TaskCard({ task, onClick }: { task: Task; onClick: () => void }) {
  const t = useT();
  const sCfg = statusConfig(task.status, t);
  const pCfg = priorityConfig(task.priority, t);
  const isRunning = task.status === 'running' || task.status === 'in_progress';

  return (
    <Card
      className={`cursor-pointer transition-shadow hover:shadow-md overflow-visible ${
        isRunning ? 'border-l-4 border-l-blue-500 animate-pulse-subtle' : ''
      }`}
      onClick={onClick}
    >
      <CardHeader className="pb-1">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-sm font-medium leading-tight line-clamp-2">
            {task.title}
          </CardTitle>
          <Badge className={pCfg.className}>{pCfg.label}</Badge>
        </div>
      </CardHeader>
      <CardContent className="pt-0 space-y-2">
        {task.description && (
          <p className="text-xs text-muted-foreground line-clamp-2">
            {task.description}
          </p>
        )}
        <div className="flex items-center gap-1.5 flex-wrap">
          <Badge className={sCfg.className}>{sCfg.label}</Badge>
          {task.score != null && (
            <span className="text-xs font-mono text-muted-foreground">
              {task.score.toFixed(1)}{t.taskCard.scoreUnit}
            </span>
          )}
        </div>
        {(task.team_name || task.assigned_to) && (
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            {task.team_name && (
              <span className="flex items-center gap-1">
                <Users className="h-3 w-3" />
                {task.team_name}
              </span>
            )}
            {task.assigned_to && (
              <span className="flex items-center gap-1">
                <User className="h-3 w-3" />
                {task.assigned_to}
              </span>
            )}
          </div>
        )}
        {task.tags?.length > 0 && (
          <div className="flex items-center gap-1 flex-wrap">
            {task.tags.map((tag) => (
              <Badge key={tag} variant="outline" className="text-[10px] px-1.5 py-0">
                {tag}
              </Badge>
            ))}
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          <RelativeTime date={task.created_at} />
        </p>
      </CardContent>
    </Card>
  );
}
