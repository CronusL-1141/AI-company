import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { RelativeTime } from '@/components/shared/RelativeTime';
import type { Task } from '@/types';

const STATUS_CONFIG: Record<string, { label: string; className: string }> = {
  pending: { label: '待处理', className: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400' },
  running: { label: '运行中', className: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400' },
  completed: { label: '已完成', className: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400' },
  failed: { label: '失败', className: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400' },
};

const PRIORITY_CONFIG: Record<string, { label: string; className: string }> = {
  critical: { label: '紧急', className: 'bg-red-500 text-white' },
  high: { label: '高', className: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-400' },
  medium: { label: '中', className: 'bg-sky-100 text-sky-800 dark:bg-sky-900/30 dark:text-sky-400' },
  low: { label: '低', className: 'bg-gray-100 text-gray-600 dark:bg-gray-800/50 dark:text-gray-400' },
};

export function statusConfig(status: string) {
  return STATUS_CONFIG[status] ?? { label: status, className: '' };
}

export function priorityConfig(priority: string) {
  return PRIORITY_CONFIG[priority] ?? { label: priority, className: '' };
}

export function TaskCard({ task, onClick }: { task: Task; onClick: () => void }) {
  const sCfg = statusConfig(task.status);
  const pCfg = priorityConfig(task.priority);

  return (
    <Card
      className="cursor-pointer transition-shadow hover:shadow-md"
      onClick={onClick}
    >
      <CardHeader className="pb-2">
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
              {task.score.toFixed(1)}分
            </span>
          )}
        </div>
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
