import { useCallback, useState } from 'react';
import type { TaskWriteResponse } from '@/api/tasks';
import { taskWarningNotice, type TaskWarningNotice } from '@/lib/task-warnings';

/** Page-owned feedback survives closing a task editor or detail dialog. */
export function useTaskWriteWarnings() {
  const [warningNotice, setWarningNotice] = useState<TaskWarningNotice | null>(null);
  const recordWarnings = useCallback((response: TaskWriteResponse) => setWarningNotice(taskWarningNotice(response)), []);
  const dismissWarnings = useCallback(() => setWarningNotice(null), []);
  return { warningNotice, recordWarnings, dismissWarnings };
}
