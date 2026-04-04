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
}

export interface BriefingListResponse {
  items: Briefing[];
  total: number;
}

export function useBriefings(status: BriefingStatus | 'all' = 'pending') {
  const params = status === 'all' ? '' : `?status=${status}`;
  return useQuery({
    queryKey: ['briefings', status],
    queryFn: () => apiFetch<BriefingListResponse>(`/api/leader-briefings${params}`),
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
