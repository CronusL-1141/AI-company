import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Agent, APIResponse, APIListResponse } from '../types';

export function useAgents(teamId: string) {
  return useQuery({
    queryKey: ['teams', teamId, 'agents'],
    queryFn: () => apiFetch<APIListResponse<Agent>>(`/api/teams/${teamId}/agents`),
    enabled: !!teamId,
  });
}

export function useDeleteAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { id: string; team_id: string }) =>
      apiFetch<APIResponse<null>>(`/api/agents/${data.id}`, { method: 'DELETE' }),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ['teams', variables.team_id, 'agents'] });
    },
  });
}
