import { useState } from 'react';
import { useProjects } from '@/api/projects';
import { useTask, useWaitingUserTasks } from '@/api/tasks';
import { useT } from '@/i18n';
import { Label } from '@/components/ui/label';
import { TaskDetailDialog } from './TaskDetailDialog';
import { TaskWarnings } from './TaskWarnings';
import { useTaskWriteWarnings } from './useTaskWriteWarnings';

const ALL_PROJECTS = '';

/**
 * Tasks waiting on the user's decision, every project by default or one project.
 * One server list decides what waits (the sidebar badge's predicate, the digest's
 * rows); this only picks the project and labels each row with its project.
 */
export function WaitingUserTasks() {
  const t = useT();
  const projects = useProjects();
  const [projectId, setProjectId] = useState(ALL_PROJECTS);
  const waiting = useWaitingUserTasks(projectId);
  const [taskId, setTaskId] = useState('');
  const task = useTask(taskId);
  const { warningNotice, recordWarnings, dismissWarnings } = useTaskWriteWarnings();
  const names = new Map(projects.data?.data.map((p) => [p.id, p.name]));
  const projectLabel = (id: string | null | undefined) =>
    id ? names.get(id) ?? id.slice(0, 8) : t.taskManage.waitingNoProject;
  const items = waiting.data?.data ?? [];
  const error = projects.error || waiting.error;
  return <section className="space-y-3 rounded-lg border bg-card p-4" aria-label={t.taskManage.waitingTitle}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="text-sm font-semibold">{t.taskManage.waitingTitle}</h2>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Label htmlFor="waiting-project">{t.tasks.selectProject}</Label>
        <select id="waiting-project" className="max-w-full rounded-md border bg-background p-1.5 text-sm focus-visible:ring-2 focus-visible:ring-ring" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value={ALL_PROJECTS}>{t.common.allProjects}</option>
          {projects.data?.data.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </div>
    </div>
    <TaskWarnings notice={warningNotice} onDismiss={dismissWarnings} />
    {(projects.isLoading || waiting.isLoading) && <p className="text-xs text-muted-foreground">{t.common.loading}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(error.message)}</p>}
    {waiting.data && items.length === 0 && <p className="text-xs text-muted-foreground">{t.taskManage.waitingEmpty}</p>}
    {items.map((item) => <button key={item.id} type="button" onClick={() => setTaskId(item.id)} className="block w-full rounded-md px-2 py-2 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      {projectId === ALL_PROJECTS && <span className="mr-2 inline-block rounded bg-muted px-1.5 text-xs text-muted-foreground">{projectLabel(item.project_id)}</span>}
      {item.parent_title && <span className="text-muted-foreground">{item.parent_title} / </span>}{item.title}
      <span className="ml-2 text-xs text-rose-700 dark:text-rose-400">{t.taskManage.waitingUser}: {item.waiting_on?.reason}</span>
    </button>)}
    {task.error && <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(task.error.message)}</p>}
    <TaskDetailDialog onWritten={recordWarnings} warningNotice={warningNotice} onDismissWarnings={dismissWarnings} onOpenTask={setTaskId} key={taskId} task={task.data?.data ?? null} open={!!taskId && !!task.data?.data} onOpenChange={(open) => { if (!open) setTaskId(''); }} />
  </section>;
}
