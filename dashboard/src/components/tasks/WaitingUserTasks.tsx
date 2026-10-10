import { TaskWarnings } from './TaskWarnings';
import { useTaskWriteWarnings } from './useTaskWriteWarnings';
import { useState } from 'react';
import { useProjects } from '@/api/projects';
import { useProjectTaskDigest, useTask } from '@/api/tasks';
import { useT } from '@/i18n';
import { Label } from '@/components/ui/label';
import { TaskDetailDialog } from './TaskDetailDialog';

/** The briefing reuses the wall's waiting semantics, without creating a second decision record. */
export function WaitingUserTasks() {
  const t = useT();
  const projects = useProjects();
  const [selection, setSelection] = useState('');
  const id = selection || projects.data?.data[0]?.id || '';
  const digest = useProjectTaskDigest(id);
  const [taskId, setTaskId] = useState('');
  const task = useTask(taskId);
  const waiting = digest.data?.waiting_user ?? [];
  const { warningNotice, recordWarnings, dismissWarnings } = useTaskWriteWarnings();
  return <section className="space-y-3 rounded-lg border bg-card p-4" aria-label={t.taskManage.waitingTitle}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="text-sm font-semibold">{t.taskManage.waitingTitle}</h2>
      <div className="flex min-w-0 flex-wrap items-center gap-2"><Label htmlFor="waiting-project">{t.tasks.selectProject}</Label><select id="waiting-project" className="max-w-full rounded-md border bg-background p-1.5 text-sm focus-visible:ring-2 focus-visible:ring-ring" value={id} onChange={(e) => setSelection(e.target.value)}>{projects.data?.data.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></div>
    </div>
    <TaskWarnings notice={warningNotice} onDismiss={dismissWarnings} />
    {(projects.isLoading || digest.isLoading) && <p className="text-xs text-muted-foreground">{t.common.loading}</p>}
    {(projects.error || digest.error) && <p role="alert" className="text-sm text-destructive">{t.common.loadFailed((projects.error || digest.error)!.message)}</p>}
    {digest.data && waiting.length === 0 && <p className="text-xs text-muted-foreground">{t.taskManage.waitingEmpty}</p>}
    {waiting.map((item) => <button key={item.id} type="button" onClick={() => setTaskId(item.id)} className="block w-full rounded-md px-2 py-2 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{item.parent_title && <span className="text-muted-foreground">{item.parent_title} / </span>}{item.title}<span className="ml-2 text-xs text-rose-700 dark:text-rose-400">{t.taskManage.waitingUser}: {item.waiting_on?.reason}</span></button>)}
    {task.error && <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(task.error.message)}</p>}
    <TaskDetailDialog onWritten={recordWarnings} warningNotice={warningNotice} onDismissWarnings={dismissWarnings} onOpenTask={setTaskId} key={taskId} task={task.data?.data ?? null} open={!!taskId && !!task.data?.data} onOpenChange={(open) => { if (!open) setTaskId(''); }} />
  </section>;
}
