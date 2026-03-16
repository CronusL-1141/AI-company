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
import { useTaskMemo } from '@/api/taskMemo';
import type { MemoEntry } from '@/api/taskMemo';
import type { Task } from '@/types';

const HORIZON_LABEL: Record<string, string> = {
  short: '短期',
  mid: '中期',
  long: '长期',
};

const MEMO_TYPE_STYLE: Record<string, { label: string; className: string }> = {
  progress: { label: '进度', className: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400' },
  decision: { label: '决策', className: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400' },
  issue: { label: '问题', className: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400' },
  summary: { label: '总结', className: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400' },
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
  const { data: memoData } = useTaskMemo(task?.id);
  const memos: MemoEntry[] = memoData?.data ?? [];

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
        <div className="flex items-center gap-3 text-sm flex-wrap">
          <span className="text-muted-foreground">周期:</span>
          <span>{HORIZON_LABEL[task.horizon] ?? task.horizon}</span>
          {task.score != null && (
            <>
              <span className="text-muted-foreground">评分:</span>
              <span className="font-mono">{task.score.toFixed(1)}</span>
            </>
          )}
          {task.team_name && (
            <>
              <span className="text-muted-foreground">团队:</span>
              <span>{task.team_name}</span>
            </>
          )}
          {task.assigned_to && (
            <>
              <span className="text-muted-foreground">负责人:</span>
              <span>{task.assigned_to}</span>
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

        {memos.length > 0 && (
          <>
            <Separator />
            <div>
              <h4 className="text-sm font-medium mb-2">Memo ({memos.length})</h4>
              <div className="space-y-2 max-h-48 overflow-auto">
                {memos.map((m, i) => {
                  const style = MEMO_TYPE_STYLE[m.type] ?? MEMO_TYPE_STYLE.progress;
                  return (
                    <div key={i} className="flex gap-2 text-sm">
                      <span className="text-muted-foreground shrink-0 w-14 text-xs pt-0.5">
                        {new Date(m.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}
                      </span>
                      <Badge className={`${style.className} shrink-0 text-xs`}>{style.label}</Badge>
                      <span className="text-muted-foreground text-xs pt-0.5">{m.author}</span>
                      <span className="flex-1">{m.content}</span>
                    </div>
                  );
                })}
              </div>
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
