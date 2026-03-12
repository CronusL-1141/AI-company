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

export function useCreateAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      team_id: string;
      name: string;
      role: string;
      system_prompt?: string;
      model?: string;
    }) =>
      apiFetch<APIResponse<Agent>>(`/api/teams/${data.team_id}/agents`, {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ['teams', variables.team_id, 'agents'] });
    },
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
