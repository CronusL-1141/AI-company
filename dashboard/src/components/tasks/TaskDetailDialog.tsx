import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { statusConfig, priorityConfig } from './TaskCard';
import type { Task } from '@/types';

const HORIZON_LABEL: Record<string, string> = {
  short: '短期',
  mid: '中期',
  long: '长期',
};

function TimelineItem({ label, time }: { label: string; time: string | null }) {
  if (!time) return null;
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-muted-foreground w-16 shrink-0">{label}</span>
      <span>{new Date(time).toLocaleString('zh-CN')}</span>
    </div>
  );
}

export function TaskDetailDialog({
  task,
  open,
  onOpenChange,
}: {
  task: Task | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  if (!task) return null;
  const sCfg = statusConfig(task.status);
  const pCfg = priorityConfig(task.priority);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <div className="flex items-start gap-2">
            <DialogTitle className="flex-1">{task.title}</DialogTitle>
            <Badge className={pCfg.className}>{pCfg.label}</Badge>
            <Badge className={sCfg.className}>{sCfg.label}</Badge>
          </div>
          <DialogDescription>ID: {task.id}</DialogDescription>
        </DialogHeader>

        {/* Meta info */}
        <div className="flex items-center gap-3 text-sm">
          <span className="text-muted-foreground">周期:</span>
          <span>{HORIZON_LABEL[task.horizon] ?? task.horizon}</span>
          {task.score != null && (
            <>
              <span className="text-muted-foreground">评分:</span>
              <span className="font-mono">{task.score.toFixed(1)}</span>
            </>
          )}
        </div>

        {task.tags?.length > 0 && (
          <div className="flex items-center gap-1.5 flex-wrap">
            {task.tags.map((tag) => (
              <Badge key={tag} variant="outline">{tag}</Badge>
            ))}
          </div>
        )}

        {task.description && (
          <>
            <Separator />
            <div>
              <h4 className="text-sm font-medium mb-1">描述</h4>
              <p className="text-sm text-muted-foreground whitespace-pre-wrap">
                {task.description}
              </p>
            </div>
          </>
        )}

        {task.result && (
          <>
            <Separator />
            <div>
              <h4 className="text-sm font-medium mb-1">结果</h4>
              <pre className="text-xs bg-muted rounded-md p-3 overflow-auto max-h-48 whitespace-pre-wrap">
                {task.result}
              </pre>
            </div>
          </>
        )}

        <Separator />
        <div>
          <h4 className="text-sm font-medium mb-2">时间轴</h4>
          <div className="space-y-1">
            <TimelineItem label="创建" time={task.created_at} />
            <TimelineItem label="开始" time={task.started_at} />
            <TimelineItem label="完成" time={task.completed_at} />
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
