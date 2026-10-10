import type { TaskWarningNotice } from '@/lib/task-warnings';
import { TaskWarnings } from './TaskWarnings';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { useWriteTask, useTaskSubtasks, type TaskWriteResponse } from '@/api/tasks';
import { TaskEditor } from './TaskEditor';
import { TaskWriteFeedback } from './TaskWriteFeedback';
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
import { useT } from '@/i18n';
import type { Task } from '@/types';
import { formatDateTime, formatTime } from '@/lib/datetime';

function TimelineItem({ label, time }: { label: string; time: string | null }) {
  if (!time) return null;
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-muted-foreground w-20 shrink-0">{label}</span>
      <span>{formatDateTime(time)}</span>
    </div>
  );
}

export function TaskDetailDialog({
  task,
  open,
  onOpenChange,
  ready = true,
  loadError,
  onOpenTask,
  onWritten,
  warningNotice,
  onDismissWarnings,
}: {
  task: Task | null;
  onOpenTask: (id: string) => void;
  onWritten: (response: TaskWriteResponse) => void;
  warningNotice: TaskWarningNotice | null;
  onDismissWarnings: () => void;
  ready?: boolean;
  loadError?: Error | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const complete = useWriteTask(true);
  const children = useTaskSubtasks(task?.id ?? '', open && !task?.parent_id);
  const { data: memoData } = useTaskMemo(task?.id);
  const memos: MemoEntry[] = memoData?.data ?? [];

  if (!task) return null;
  const finish = (revision = task.revision) => complete.mutate({ id: task.id, expected_revision: revision }, { onSuccess: onWritten });
  const sCfg = statusConfig(task.status, t);
  const pCfg = priorityConfig(task.priority, t);

  const HORIZON_LABEL: Record<string, string> = {
    short: t.taskDetail.horizonShort,
    mid: t.taskDetail.horizonMid,
    long: t.taskDetail.horizonLong,
  };

  const MEMO_TYPE_STYLE: Record<string, { label: string; className: string }> = {
    progress: { label: t.taskDetail.memoTypeProgress, className: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400' },
    decision: { label: t.taskDetail.memoTypeDecision, className: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-400' },
    issue: { label: t.taskDetail.memoTypeIssue, className: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400' },
    summary: { label: t.taskDetail.memoTypeSummary, className: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400' },
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Capped height with its own scroll: a long description outgrows the viewport,
          and a fixed-positioned popup cannot be scrolled to reach what it pushes off-screen */}
      <DialogContent className="sm:max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader className="min-w-0 pr-6">
          <div className="flex items-start gap-2">
            <DialogTitle className="flex-1 min-w-0 break-words">{task.title}</DialogTitle>
            <Badge className={pCfg.className}>{pCfg.label}</Badge>
            <Badge className={sCfg.className}>{sCfg.label}</Badge>
          </div>
          <DialogDescription>ID: {task.id}</DialogDescription>
        </DialogHeader>

        {loadError && <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(loadError.message)}</p>}
        {!ready && !loadError && <p className="text-xs text-muted-foreground">{t.common.loading}</p>}
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" disabled={!ready} onClick={() => setEditing(!editing)}>{t.taskManage.edit}</Button>
          {!editing && task.status !== 'completed' && <Button type="button" disabled={complete.isPending || !ready} onClick={() => finish()}>{t.taskManage.complete}</Button>}
        </div>
        <TaskWarnings notice={warningNotice} onDismiss={onDismissWarnings} />
        {!editing && <TaskWriteFeedback error={complete.error} onOpenTask={onOpenTask} pending={complete.isPending} onAcceptRevision={finish} />}
        {editing ? <TaskEditor key={task.id} task={task} onOpenTask={onOpenTask} onWritten={onWritten} onDone={() => setEditing(false)} /> : <>
        {task.waiting_on && <p className="text-sm text-rose-700 dark:text-rose-400">{task.waiting_on.kind === 'user' ? t.taskManage.waitingUser : t.taskManage.agent}: {task.waiting_on.reason}</p>}
        {task.executor_stopped && task.status !== 'completed' && <p className="text-sm text-amber-700 dark:text-amber-400">{t.taskManage.stopped}</p>}
        {!!task.management_hints?.length && <ul className="space-y-1 text-xs text-muted-foreground">{task.management_hints.map((hint) => <li key={hint}>{hint}</li>)}</ul>}
        {task.status_note && <p className="text-sm whitespace-pre-wrap break-words">{task.status_note}</p>}
        {task.parent_id && <Button type="button" size="sm" variant="outline" onClick={() => onOpenTask(task.parent_id!)}>{t.taskManage.backToParent}</Button>}
        {children.isLoading && <p className="text-xs text-muted-foreground">{t.common.loading}</p>}
        {children.error && <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(children.error.message)}</p>}
        {!!children.data?.data.subtasks.length && <section className="space-y-2" aria-label={t.taskManage.progress}>
          <h4 className="text-sm font-medium">{t.taskManage.progress}</h4>
          <ul className="space-y-1">{children.data.data.subtasks.map((child) => <li key={child.id}><button type="button" onClick={() => onOpenTask(child.id)} className="flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><span className="min-w-0 break-words">{child.title}</span><Badge className={statusConfig(child.status, t).className}>{statusConfig(child.status, t).label}</Badge></button></li>)}</ul>
        </section>}
        {!!task.depends_on?.length && <p className="text-xs break-all">{t.taskManage.dependencies}: {task.depends_on.join(', ')}</p>}
        {task.subtask_progress && <p className="text-sm">{t.taskManage.progress} {task.subtask_progress.completed}/{task.subtask_progress.total}</p>}
        {/* Meta info */}
        <div className="flex items-center gap-3 text-sm flex-wrap">
          <span className="text-muted-foreground">{t.taskDetail.fieldHorizon}</span>
          <span>{HORIZON_LABEL[task.horizon] ?? task.horizon}</span>
          {task.score != null && (
            <>
              <span className="text-muted-foreground">{t.taskDetail.fieldScore}</span>
              <span className="font-mono">{task.score.toFixed(1)}</span>
            </>
          )}
          {task.team_name && (
            <>
              <span className="text-muted-foreground">{t.taskDetail.fieldTeam}</span>
              <span>{task.team_name}</span>
            </>
          )}
          {task.assigned_to && (
            <>
              <span className="text-muted-foreground">{t.taskDetail.fieldAssignedTo}</span>
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
            <div className="min-w-0">
              <h4 className="text-sm font-medium mb-1">{t.taskDetail.sectionDesc}</h4>
              <p className="text-sm text-muted-foreground whitespace-pre-wrap break-words">
                {task.description}
              </p>
            </div>
          </>
        )}

        {task.result && (
          <>
            <Separator />
            <div className="min-w-0">
              <h4 className="text-sm font-medium mb-1">{t.taskDetail.sectionResult}</h4>
              <pre className="text-xs bg-muted rounded-md p-3 overflow-y-auto max-h-48 whitespace-pre-wrap break-words">
                {task.result}
              </pre>
            </div>
          </>
        )}

        {memos.length > 0 && (
          <>
            <Separator />
            <div className="min-w-0">
              <h4 className="text-sm font-medium mb-2">{t.taskDetail.sectionMemo} ({memos.length})</h4>
              <div className="space-y-2 max-h-48 overflow-y-auto">
                {memos.map((m, i) => {
                  const style = MEMO_TYPE_STYLE[m.type] ?? MEMO_TYPE_STYLE.progress;
                  // Meta (time / type / author) sits on its own line at every
                  // width - sharing the row squeezed the body to a sliver next
                  // to a long author name (user-reported).
                  return (
                    <div key={i} className="flex flex-wrap gap-x-2 gap-y-0.5 text-sm min-w-0">
                      <span className="text-muted-foreground shrink-0 text-xs pt-0.5">
                        {formatTime(m.timestamp, { hour: '2-digit', minute: '2-digit' })}
                      </span>
                      <Badge className={`${style.className} shrink-0 text-xs`}>{style.label}</Badge>
                      <span className="text-muted-foreground shrink-0 text-xs pt-0.5">{m.author}</span>
                      <span className="min-w-0 basis-full break-words whitespace-pre-wrap">
                        {m.content}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </>
        )}

        <Separator />
        <div>
          <h4 className="text-sm font-medium mb-2">{t.taskDetail.sectionTimeline}</h4>
          <div className="space-y-1">
            <TimelineItem label={t.taskDetail.timelineCreated} time={task.created_at} />
            <TimelineItem label={t.taskDetail.timelineStarted} time={task.started_at} />
            <TimelineItem label={t.taskDetail.timelineCompleted} time={task.completed_at} />
          </div>
        </div>
        </>}
      </DialogContent>
    </Dialog>
  );
}
