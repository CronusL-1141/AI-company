import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';

export interface ToolUsageItem {
  tool_name: string;
  count: number;
}

export interface AgentProductivityItem {
  agent_id: string;
  agent_name: string;
  activity_count: number;
  tools_used: number;
  last_active: string;
}

export interface TimelineItem {
  hour: string;
  count: number;
}

export interface TeamOverview {
  total_activities: number;
  total_agents: number;
  active_agents: number;
  tool_distribution: ToolUsageItem[];
  agent_productivity: AgentProductivityItem[];
}

interface ApiResponse<T> {
  success: boolean;
  data: T;
}

export function useToolUsage(teamId?: string) {
  return useQuery({
    queryKey: ['analytics', 'tool-usage', teamId],
    queryFn: async () => {
      const res = await apiFetch<ApiResponse<ToolUsageItem[]>>(
        `/api/analytics/tool-usage${teamId ? `?team_id=${teamId}` : ''}`,
      );
      return res.data;
    },
    refetchInterval: 30_000,
  });
}

export function useAgentProductivity(teamId?: string) {
  return useQuery({
    queryKey: ['analytics', 'agent-productivity', teamId],
    queryFn: async () => {
      const res = await apiFetch<ApiResponse<AgentProductivityItem[]>>(
        `/api/analytics/agent-productivity${teamId ? `?team_id=${teamId}` : ''}`,
      );
      return res.data;
    },
    refetchInterval: 30_000,
  });
}

export function useActivityTimeline(teamId?: string, hours = 24) {
  return useQuery({
    queryKey: ['analytics', 'timeline', teamId, hours],
    queryFn: async () => {
      const res = await apiFetch<ApiResponse<TimelineItem[]>>(
        `/api/analytics/timeline${teamId ? `?team_id=${teamId}&hours=${hours}` : `?hours=${hours}`}`,
      );
      return res.data;
    },
    refetchInterval: 60_000,
  });
}

export interface TaskCompletion {
  total_tasks: number;
  completed_tasks: number;
  completion_rate: number;
  avg_completion_hours: number | null;
}

export interface AgentUtilizationItem {
  agent_id: string;
  agent_name: string;
  activity_count: number;
  tools_used: number;
  span_hours: number;
  activities_per_hour: number;
  first_active: string | null;
  last_active: string | null;
}

export interface EfficiencyMetrics {
  task_completion: TaskCompletion;
  avg_tools_per_task: number | null;
  agent_utilization: AgentUtilizationItem[];
  top_agents: AgentUtilizationItem[];
}

export function useEfficiencyMetrics(teamId?: string) {
  return useQuery({
    queryKey: ['analytics', 'efficiency', teamId],
    queryFn: async () => {
      const res = await apiFetch<ApiResponse<EfficiencyMetrics>>(
        `/api/analytics/efficiency${teamId ? `?team_id=${teamId}` : ''}`,
      );
      return res.data;
    },
    refetchInterval: 30_000,
  });
}

export function useTeamOverview(teamId: string) {
  return useQuery({
    queryKey: ['analytics', 'team-overview', teamId],
    queryFn: async () => {
      const res = await apiFetch<ApiResponse<TeamOverview>>(
        `/api/analytics/team-overview?team_id=${teamId}`,
      );
      return res.data;
    },
    enabled: !!teamId,
    refetchInterval: 30_000,
  });
}
