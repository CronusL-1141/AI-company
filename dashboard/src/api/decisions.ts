import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';

export interface DecisionEvent {
  id: string;
  type: string;
  source: string;
  data: Record<string, any>;
  timestamp: string;
}

export interface AgentIntent {
  agent_id: string;
  agent_name: string;
  tool_name: string;
  intent_summary: string;
  input_preview: string;
  timestamp: string | null;
}

export function useDecisions(teamId: string, limit = 50) {
  return useQuery({
    queryKey: ['decisions', teamId],
    queryFn: () => apiFetch<{ data: DecisionEvent[] }>(`/api/decisions?team_id=${teamId}&limit=${limit}`),
    enabled: !!teamId,
    refetchInterval: 15000,
  });
}

export function useAgentIntents(teamId: string) {
  return useQuery({
    queryKey: ['agent-intents', teamId],
    queryFn: () => apiFetch<{ data: AgentIntent[] }>(`/api/teams/${teamId}/agent-intents`),
    enabled: !!teamId,
    refetchInterval: 8000,
  });
}
