import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { APIListResponse } from '@/types';

export interface AgentActivity {
  id: string;
  agent_id: string;
  session_id: string;
  tool_name: string;
  input_summary: string;
  output_summary: string;
  timestamp: string;
}

export function useAgentActivities(agentId?: string, limit = 50) {
  return useQuery({
    queryKey: ['activities', agentId, limit],
    queryFn: () =>
      apiFetch<APIListResponse<AgentActivity>>(
        `/api/agents/${agentId}/activities?limit=${limit}`,
      ),
    enabled: !!agentId,
    refetchInterval: 5000,
  });
}
