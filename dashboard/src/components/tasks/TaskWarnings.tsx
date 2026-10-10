import { Alert, AlertTitle, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import type { TaskWarningNotice } from '@/lib/task-warnings';
import { useT } from '@/i18n';

export function TaskWarnings({ notice, onDismiss }: { notice: TaskWarningNotice | null; onDismiss: () => void }) {
  const t = useT();
  if (!notice) return null;
  return <Alert aria-label={t.taskManage.writeWarnings} className="border-amber-400/50 bg-amber-50/40 dark:bg-amber-950/20">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <AlertTitle className="min-w-0 break-words">{t.taskManage.writeWarnings} · {notice.taskTitle}</AlertTitle>
      <Button type="button" variant="outline" size="sm" onClick={onDismiss}>{t.taskManage.dismissWarnings}</Button>
    </div>
    <AlertDescription><ul className="max-h-48 space-y-1 overflow-y-auto break-words whitespace-pre-wrap">{notice.messages.map((message) => <li key={message}>{message}</li>)}</ul></AlertDescription>
  </Alert>;
}
