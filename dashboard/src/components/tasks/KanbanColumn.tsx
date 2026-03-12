import { Badge } from '@/components/ui/badge';
import { TaskCard } from './TaskCard';
import type { Task } from '@/types';

interface KanbanColumnProps {
  title: string;
  count: number;
  badgeClassName: string;
  tasks: Task[];
  onTaskClick: (task: Task) => void;
}

export function KanbanColumn({ title, count, badgeClassName, tasks, onTaskClick }: KanbanColumnProps) {
  return (
    <div className="flex flex-col min-w-[260px] flex-1">
      <div className="flex items-center gap-2 mb-3 px-1">
        <h3 className="text-sm font-medium">{title}</h3>
        <Badge className={badgeClassName}>{count}</Badge>
      </div>
      <div className="flex flex-col gap-2 overflow-y-auto max-h-[calc(100vh-280px)] pr-1">
        {tasks.length === 0 ? (
          <p className="text-xs text-muted-foreground text-center py-8">
            暂无任务
          </p>
        ) : (
          tasks.map((task) => (
            <TaskCard key={task.id} task={task} onClick={() => onTaskClick(task)} />
          ))
        )}
      </div>
    </div>
  );
}
