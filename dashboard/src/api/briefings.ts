import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';

export type BriefingUrgency = 'high' | 'medium' | 'low';
export type BriefingStatus = 'pending' | 'resolved' | 'dismissed';

export interface Briefing {
  id: string;
  title: string;
  description: string;
  options: string;
  recommendation: string;
  urgency: BriefingUrgency;
  status: BriefingStatus;
  resolution?: string | null;
  created_at: string;
  resolved_at?: string | null;
  project_id?: string | null;
}

export interface BriefingListResponse {
  items: Briefing[];
  total: number;
}

export function useBriefings(status: BriefingStatus | 'all' = 'pending', projectId?: string) {
  const params = new URLSearchParams();
  if (status !== 'all') params.set('status', status);
  if (projectId) params.set('project_id', projectId);
  const qs = params.toString() ? `?${params.toString()}` : '';
  return useQuery({
    queryKey: ['briefings', status, projectId ?? ''],
    queryFn: () => apiFetch<BriefingListResponse>(`/api/leader-briefings${qs}`),
  });
}

export function useResolveBriefing() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, resolution }: { id: string; resolution: string }) =>
      apiFetch<{ data: Briefing; message: string }>(`/api/leader-briefings/${id}/resolve`, {
        method: 'PUT',
        body: JSON.stringify({ resolution }),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['briefings'] });
    },
  });
}

export function useDismissBriefing() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<{ data: Briefing; message: string }>(`/api/leader-briefings/${id}/dismiss`, {
        method: 'PUT',
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['briefings'] });
    },
  });
}
