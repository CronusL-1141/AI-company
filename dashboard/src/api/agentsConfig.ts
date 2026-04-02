import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';

export interface AgentTemplate {
  filename: string;
  name: string;
  description: string;
  model: string;
  color: string;
  prompt: string;
}

export interface AgentTemplateUpdate {
  name: string;
  description: string;
  model: string;
  color: string;
  prompt: string;
}

export function useAgentTemplates() {
  return useQuery({
    queryKey: ['agent-templates-config'],
    queryFn: () =>
      apiFetch<{ data: AgentTemplate[] }>('/api/agents-config').then((r) => r.data),
  });
}

export function useUpdateAgentTemplate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ filename, data }: { filename: string; data: AgentTemplateUpdate }) =>
      apiFetch<{ data: AgentTemplate }>(`/api/agents-config/${encodeURIComponent(filename)}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['agent-templates-config'] });
    },
  });
}
