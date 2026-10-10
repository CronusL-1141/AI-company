import type { TaskWrite } from '@/api/tasks';
import type { Task } from '@/types';

/** Compare against the fixed editing snapshot, never a refetched server object.
 * Only dirty fields are sent; empty dirty values retain the API's explicit clear semantics.
 */
export function taskEditPatch(initial: Task, values: TaskWrite): TaskWrite {
  const patch: TaskWrite = {};
  for (const field of ['title', 'description', 'result', 'status', 'status_note'] as const) {
    const next = values[field];
    if (next !== undefined && next !== (initial[field] ?? '')) patch[field] = next;
  }
  if (values.horizon !== undefined && values.horizon !== initial.horizon) patch.horizon = values.horizon;
  if (values.assigned_to !== undefined && (values.assigned_to || null) !== (initial.assigned_to || null)) {
    patch.assigned_to = values.assigned_to || null;
  }
  if (values.parent_id !== undefined && values.parent_id !== (initial.parent_id ?? null)) patch.parent_id = values.parent_id;
  if (values.depends_on !== undefined) {
    const oldIds = [...new Set(initial.depends_on ?? [])].sort();
    const newIds = [...new Set(values.depends_on)].sort();
    if (JSON.stringify(oldIds) !== JSON.stringify(newIds)) patch.depends_on = values.depends_on;
  }
  if (values.waiting_on !== undefined) {
    const waitingKey = (value: Task['waiting_on']) => value
      ? JSON.stringify([value.kind, value.target_id, value.reason]) : null;
    if (waitingKey(values.waiting_on) !== waitingKey(initial.waiting_on)) patch.waiting_on = values.waiting_on;
  }
  return patch;
}
