import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Memory, APIListResponse } from '../types';

export function useTeamKnowledge(teamId: string, type?: string) {
  const params = new URLSearchParams({ limit: '50' });
  if (type) params.set('type', type);
  return useQuery({
    queryKey: ['team-knowledge', teamId, type],
    queryFn: () =>
      apiFetch<APIListResponse<Memory>>(`/api/teams/${teamId}/knowledge?${params.toString()}`),
    enabled: !!teamId,
  });
}

export function useAgentExperience(agentId: string) {
  return useQuery({
    queryKey: ['agent-experience', agentId],
    queryFn: () =>
      apiFetch<APIListResponse<Memory>>(`/api/agents/${agentId}/experience?limit=50`),
    enabled: !!agentId,
  });
}
