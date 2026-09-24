import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';

export type BriefingUrgency = 'high' | 'medium' | 'low';
export type BriefingStatus = 'pending' | 'resolved' | 'dismissed' | 'expired';

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
  tags?: string[] | null;
}

export interface BriefingListResponse {
  items: Briefing[];
  total: number;
}

// Pending items from permission denials are written by a hook, not by anyone
// waiting on the user; real_only drops them (same rule as the API's notices).
export function briefingsPath(
  status: BriefingStatus | 'all', projectId?: string, tag?: string, realOnly = false,
): string {
  const params = new URLSearchParams();
  params.set('status', status);
  if (projectId) params.set('project_id', projectId);
  if (tag) params.set('tag', tag);
  if (realOnly) params.set('real_only', 'true');
  return `/api/leader-briefings?${params.toString()}`;
}

export function useBriefings(
  status: BriefingStatus | 'all' = 'pending',
  projectId?: string,
  tag?: string,
  realOnly = false,
) {
  const qs = briefingsPath(status, projectId, tag, realOnly).slice('/api/leader-briefings'.length);
  return useQuery({
    queryKey: ['briefings', status, projectId ?? '', tag ?? '', realOnly],
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
      void qc.invalidateQueries({ queryKey: ['notices'] });  // pending counts
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
      void qc.invalidateQueries({ queryKey: ['notices'] });  // pending counts
    },
  });
}
