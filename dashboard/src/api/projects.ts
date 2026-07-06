import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Project, Phase, APIResponse, APIListResponse } from '@/types';

export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: () => apiFetch<APIListResponse<Project>>('/api/projects'),
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: ['projects', id],
    queryFn: () => apiFetch<APIResponse<Project>>(`/api/projects/${id}`),
    enabled: !!id,
  });
}

export function useProjectPhases(projectId: string) {
  return useQuery({
    queryKey: ['projects', projectId, 'phases'],
    queryFn: () =>
      apiFetch<APIListResponse<Phase>>(`/api/projects/${projectId}/phases`),
    enabled: !!projectId,
  });
}

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      name: string;
      root_path?: string;
      description?: string;
      config?: Record<string, unknown>;
    }) =>
      apiFetch<APIResponse<Project>>('/api/projects', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<APIResponse<null>>(`/api/projects/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export interface SummaryLeader {
  name: string;
  model: string;
  status: string;
  session_id: string;
  current_task: string;
  last_active_at: string | null;
}

export interface ProjectSummary {
  status: 'active' | 'inactive';
  active_teams: number;
  pending_tasks: number;
  running_tasks: number;
  /** 该项目下出现过的去重 CC 会话数（agents.session_id 足迹） */
  session_count?: number;
  last_activity_at?: string | null;
  /** 按 project_id 直出的最新 Leader（不经 team 链——Leader 行可能寄生在跨项目 workflow 队） */
  leader?: SummaryLeader | null;
  top_tasks: { title: string; priority: string }[];
}

export function useProjectSummary(projectId: string) {
  return useQuery({
    queryKey: ['projects', projectId, 'summary'],
    queryFn: () => apiFetch<ProjectSummary>(`/api/projects/${projectId}/summary`),
    enabled: !!projectId,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}
