import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';

export interface MemoEntry {
  timestamp: string;
  author: string;
  content: string;
  type: 'progress' | 'decision' | 'issue' | 'summary';
}

interface MemoResponse {
  success: boolean;
  data: MemoEntry[];
}

export function useTaskMemo(taskId?: string) {
  return useQuery({
    queryKey: ['task-memo', taskId],
    queryFn: () => apiFetch<MemoResponse>(`/api/tasks/${taskId}/memo`),
    enabled: !!taskId,
  });
}
