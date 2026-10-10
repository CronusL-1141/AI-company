import { useState } from 'react';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { useProjectTaskWall, useWriteTask, type TaskWriteResponse } from '@/api/tasks';
import { useT } from '@/i18n';
import { taskEditPatch } from '@/lib/task-editor';
import type { Task } from '@/types';
import { managementDraft, managementPayload, TaskManagementFields } from './TaskManagementFields';
import { TaskWriteFeedback } from './TaskWriteFeedback';
import { statusConfig } from './TaskCard';

export function TaskEditor({ task, onDone, onOpenTask, onWritten }: { task: Task; onDone: () => void; onOpenTask: (id: string) => void; onWritten: (response: TaskWriteResponse) => void }) {
  const t = useT();
  const [title, setTitle] = useState(task.title);
  const [description, setDescription] = useState(task.description ?? '');
  const [result, setResult] = useState(task.result ?? '');
  const [assigned, setAssigned] = useState(task.assigned_to ?? '');
  const [status, setStatus] = useState(task.status);
  const [initial] = useState(() => structuredClone(task));
  const [draft, setDraft] = useState(() => managementDraft(task));
  const [revision, setRevision] = useState(task.revision);
  const write = useWriteTask();
  const wall = useProjectTaskWall(task.project_id ?? '');
  const options = Object.values(wall.data?.wall ?? {}).flat().flatMap((row) => [row, ...(row.subtasks ?? [])]).filter((row) => row.id !== task.id);
  const patch = taskEditPatch(initial, { title: title.trim(), description, result, status,
    assigned_to: assigned.trim() || null, ...managementPayload(draft) });
  const submit = (expected = revision) => write.mutate({ id: task.id, ...patch, expected_revision: expected }, { onSuccess: (response) => { onWritten(response); onDone(); } });
  return <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); submit(); }}>
    <div className="space-y-1"><Label htmlFor="edit-title">{t.tasks.taskTitle}</Label><Input id="edit-title" value={title} onChange={(e) => setTitle(e.target.value)} /></div>
    <div className="space-y-1"><Label htmlFor="edit-desc">{t.tasks.taskDesc}</Label><Textarea id="edit-desc" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
    <div className="space-y-1"><Label htmlFor="edit-assigned">{t.taskManage.assigned}</Label><Input id="edit-assigned" value={assigned} onChange={(e) => setAssigned(e.target.value)} /></div>
    <div className="space-y-1"><Label htmlFor="edit-status">{t.taskManage.status}</Label><select id="edit-status" className="h-9 w-full rounded-md border bg-background px-3 text-sm focus-visible:ring-2 focus-visible:ring-ring" value={status} onChange={(e) => setStatus(e.target.value)}>{['pending', 'running', 'blocked', 'failed', 'completed'].map((s) => <option key={s} value={s}>{statusConfig(s, t).label}</option>)}</select></div>
    <TaskManagementFields draft={draft} onChange={setDraft} prefix="edit-task" options={options} />
    <div className="space-y-1"><Label htmlFor="edit-result">{t.taskManage.result}</Label><Textarea id="edit-result" value={result} onChange={(e) => setResult(e.target.value)} /></div>
    <p className="text-xs text-muted-foreground">{t.taskManage.clearHint}</p>
    <TaskWriteFeedback error={write.error} onOpenTask={onOpenTask} pending={write.isPending} editedFields={Object.keys(patch)} onAcceptRevision={(next) => { setRevision(next); submit(next); }} />
    <div className="flex flex-wrap gap-2"><Button type="submit" disabled={write.isPending || !title.trim() || Object.keys(patch).length === 0}>{write.isPending ? t.common.submitting : t.taskManage.save}</Button><Button type="button" variant="outline" onClick={onDone}>{t.common.cancel}</Button></div>
  </form>;
}
