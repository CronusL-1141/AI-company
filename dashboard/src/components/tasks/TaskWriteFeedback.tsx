import { ApiHttpError } from '@/api/client';
import { Button } from '@/components/ui/button';
import { useT } from '@/i18n';
import type { Task } from '@/types';

interface ConflictDetail {
  message?: string;
  current_revision?: number;
  current_task?: Task;
  open_subtasks?: { id: string; title: string }[];
}
export function TaskWriteFeedback({ error, onAcceptRevision, onOpenTask, pending = false, editedFields }: {
  error: Error | null; onAcceptRevision: (revision: number) => void;
  onOpenTask: (id: string) => void; pending?: boolean; editedFields?: string[];
}) {
  const t = useT();
  if (!error) return null;
  const detail = error instanceof ApiHttpError && error.detail && typeof error.detail === 'object' ? error.detail as ConflictDetail : null;
  return <div role="alert" className="space-y-2 rounded-md border border-destructive/40 p-3 text-sm">
    <p className="text-destructive">{detail?.current_revision != null ? t.taskManage.conflict : error.message}</p>
    {detail?.open_subtasks && <div><p>{t.taskManage.openSubtasks}</p><ul className="list-disc pl-5">{detail.open_subtasks.map((child) => <li key={child.id} className="break-words"><button type="button" onClick={() => onOpenTask(child.id)} className="rounded-md px-1 py-1 text-left underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{child.title}</button></li>)}</ul></div>}
    {detail?.current_task && <div className="space-y-1 break-words text-xs text-muted-foreground">
      <p>{detail.current_task.title} · {detail.current_task.status} · revision {detail.current_revision}</p>
      <p className="whitespace-pre-wrap">{detail.current_task.description}</p>
      <p>{t.taskManage.horizon}: {detail.current_task.horizon} · {t.taskManage.assigned}: {detail.current_task.assigned_to || '—'}</p>
      <p className="whitespace-pre-wrap">{t.taskManage.result}: {detail.current_task.result || '—'}</p>
      <p>{t.taskManage.note}: {detail.current_task.status_note || '—'}</p>
      <p>{t.taskManage.parent}: {detail.current_task.parent_id || '—'}</p>
      <p>{t.taskManage.dependencies}: {detail.current_task.depends_on?.join(', ') || '—'}</p>
      <p>{t.taskManage.waiting}: {detail.current_task.waiting_on?.reason || '—'}</p>
    </div>}
    {detail?.current_revision != null && editedFields && <p className="text-xs text-muted-foreground">{t.taskManage.retryEditedFields} {editedFields.map((field) => t.taskManage.editFields[field] ?? field).join(', ')}</p>}
    {detail?.current_revision != null && <Button disabled={pending} type="button" size="sm" variant="outline" onClick={() => onAcceptRevision(detail.current_revision!)}>{t.taskManage.retry}</Button>}
  </div>;
}
