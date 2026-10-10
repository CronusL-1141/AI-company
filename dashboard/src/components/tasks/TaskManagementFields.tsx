import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { useT } from '@/i18n';
import type { Task } from '@/types';
import type { TaskWrite } from '@/api/tasks';

export interface ManagementDraft {
  parent: string;
  dependencies: string;
  horizon: Task['horizon'];
  waiting: 'none' | 'user' | 'agent';
  target: string;
  reason: string;
  note: string;
}
export function managementDraft(task?: Task): ManagementDraft {
  return { parent: task?.parent_id ?? '', dependencies: task?.depends_on?.join('\n') ?? '',
    horizon: task?.horizon ?? 'short', waiting: task?.waiting_on?.kind ?? 'none',
    target: task?.waiting_on?.target_id ?? '', reason: task?.waiting_on?.reason ?? '',
    note: task?.status_note ?? '' };
}
export function managementValid(draft: ManagementDraft): boolean {
  return draft.waiting === 'none' || (!!draft.reason.trim() && (draft.waiting !== 'agent' || !!draft.target.trim()));
}
export function managementPayload(draft: ManagementDraft): TaskWrite {
  return { parent_id: draft.parent.trim() || null,
    depends_on: [...new Set(draft.dependencies.split(/[,\n]/).map((id) => id.trim()).filter(Boolean))],
    horizon: draft.horizon, status_note: draft.note,
    waiting_on: draft.waiting === 'none' ? null : {
      kind: draft.waiting, target_id: draft.target.trim(), reason: draft.reason.trim(),
    } };
}
const selectClass = 'h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring';
export function TaskManagementFields({ draft, onChange, prefix, options = [] }: {
  draft: ManagementDraft; onChange: (draft: ManagementDraft) => void; prefix: string; options?: Task[];
}) {
  const t = useT();
  function change<K extends keyof ManagementDraft>(key: K, value: ManagementDraft[K]) {
    onChange({ ...draft, [key]: value });
  }
  const parents = options.filter((task) => !task.parent_id && task.status !== 'completed');
  const dependencies = draft.dependencies.split(/[,\n]/).map((id) => id.trim()).filter(Boolean);
  const unknownDependencies = dependencies.filter((id) => !options.some((task) => task.id === id));
  return <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
    <div className="space-y-1 sm:col-span-2">
      <Label htmlFor={`${prefix}-parent-choice`}>{t.taskManage.parentChoice}</Label>
      <select id={`${prefix}-parent-choice`} className={selectClass} value={draft.parent} onChange={(e) => change('parent', e.target.value)}>
        <option value="">{t.taskManage.topLevel}</option>
        {draft.parent && !parents.some((task) => task.id === draft.parent) && <option value={draft.parent}>{t.taskManage.notLoaded} · {draft.parent.slice(0, 8)}</option>}
        {parents.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}
      </select>
      <p className="text-xs text-muted-foreground">{t.taskManage.parentHint}</p>
    </div>
    <fieldset className="space-y-2 sm:col-span-2">
      <legend className="text-sm font-medium">{t.taskManage.dependenciesChoice}</legend>
      <div className="max-h-36 space-y-1 overflow-y-auto rounded-md border p-2">
        {options.length === 0 && <p className="text-xs text-muted-foreground">{t.taskManage.notLoaded}</p>}
        {options.map((task) => <label key={task.id} className="flex items-start gap-2 py-1 text-sm">
          <input type="checkbox" className="mt-0.5 focus-visible:ring-2 focus-visible:ring-ring" checked={dependencies.includes(task.id)} onChange={(e) => change('dependencies', (e.target.checked ? [...dependencies, task.id] : dependencies.filter((id) => id !== task.id)).join('\n'))} />
          <span className="break-words">{task.title}</span>
        </label>)}
        {unknownDependencies.map((id) => <label key={id} className="flex items-start gap-2 py-1 text-xs text-muted-foreground"><input type="checkbox" checked onChange={() => change('dependencies', dependencies.filter((value) => value !== id).join('\n'))} className="focus-visible:ring-2 focus-visible:ring-ring" /><span className="break-all">{t.taskManage.notLoaded} · {id}</span></label>)}
      </div>
      <div className="flex flex-wrap items-center gap-2"><Button type="button" size="sm" variant="outline" disabled={dependencies.length === 0} onClick={() => change('dependencies', '')}>{t.taskManage.clearDependencies}</Button><p className="text-xs text-muted-foreground">{t.taskManage.loadedScope}</p></div>
    </fieldset>
    <details className="rounded-md border p-2 sm:col-span-2">
      <summary className="cursor-pointer text-xs text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring">{t.taskManage.advancedRelations}</summary>
      <div className="mt-2 space-y-2">
        <Label htmlFor={`${prefix}-parent`}>{t.taskManage.parent}</Label>
        <Input id={`${prefix}-parent`} value={draft.parent} onChange={(e) => change('parent', e.target.value)} />
        <Label htmlFor={`${prefix}-deps`}>{t.taskManage.dependencies}</Label>
        <Textarea id={`${prefix}-deps`} value={draft.dependencies} onChange={(e) => change('dependencies', e.target.value)} rows={2} />
        <p className="text-xs text-muted-foreground">{t.taskManage.dependenciesHint}</p>
      </div>
    </details>
    <div className="space-y-1">
      <Label htmlFor={`${prefix}-horizon`}>{t.taskManage.horizon}</Label>
      <select id={`${prefix}-horizon`} className={selectClass} value={draft.horizon} onChange={(e) => change('horizon', e.target.value as Task['horizon'])}>
        <option value="short">{t.tasks.horizonShort}</option><option value="mid">{t.tasks.horizonMid}</option><option value="long">{t.tasks.horizonLong}</option>
      </select>
    </div>
    <div className="space-y-1">
      <Label htmlFor={`${prefix}-waiting`}>{t.taskManage.waiting}</Label>
      <select id={`${prefix}-waiting`} className={selectClass} value={draft.waiting} onChange={(e) => change('waiting', e.target.value as ManagementDraft['waiting'])}>
        <option value="none">{t.taskManage.none}</option><option value="user">{t.taskManage.user}</option><option value="agent">{t.taskManage.agent}</option>
      </select>
    </div>
    {draft.waiting !== 'none' && <>
      <div className="space-y-1 sm:col-span-2"><Label htmlFor={`${prefix}-target`}>{t.taskManage.target}</Label><Input required={draft.waiting === 'agent'} id={`${prefix}-target`} value={draft.target} onChange={(e) => change('target', e.target.value)} /></div>
      <div className="space-y-1 sm:col-span-2"><Label htmlFor={`${prefix}-reason`}>{t.taskManage.reason}</Label><Input maxLength={2000} required id={`${prefix}-reason`} value={draft.reason} onChange={(e) => change('reason', e.target.value)} /></div>
    </>}
    <div className="space-y-1 sm:col-span-2"><Label htmlFor={`${prefix}-note`}>{t.taskManage.note}</Label><Input id={`${prefix}-note`} value={draft.note} onChange={(e) => change('note', e.target.value)} /><p className="text-xs text-muted-foreground">{t.taskManage.clearHint}</p></div>
  </div>;
}
