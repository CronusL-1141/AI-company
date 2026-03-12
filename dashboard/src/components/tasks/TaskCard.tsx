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

export function statusConfig(status: string) {
  return STATUS_CONFIG[status] ?? { label: status, className: '' };
}

export function TaskCard({ task, onClick }: { task: Task; onClick: () => void }) {
  const cfg = statusConfig(task.status);

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
          <Badge className={cfg.className}>{cfg.label}</Badge>
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        {task.description && (
          <p className="text-xs text-muted-foreground line-clamp-2 mb-2">
            {task.description}
          </p>
        )}
        <p className="text-xs text-muted-foreground">
          <RelativeTime date={task.created_at} />
        </p>
      </CardContent>
    </Card>
  );
}
