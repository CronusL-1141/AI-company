import type { TaskWriteResponse } from '@/api/tasks';

export interface TaskWarningNotice {
  taskId: string;
  taskTitle: string;
  messages: string[];
}

/** Both current warnings and the older team-create warning remain visible. */
export function taskWarningNotice(response: TaskWriteResponse): TaskWarningNotice | null {
  const messages = [...new Set([...(response.warnings ?? []), response._warning ?? '']
    .filter((message) => typeof message === 'string' && message.trim().length > 0)
    .map((message) => message.trim()))];
  return messages.length ? { taskId: response.data.id, taskTitle: response.data.title, messages } : null;
}
