import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Task, TaskWallResponse, APIResponse, APIListResponse } from '../types';

export function useTasks(teamId: string) {
  return useQuery({
    queryKey: ['teams', teamId, 'tasks'],
    queryFn: () => apiFetch<APIListResponse<Task>>(`/api/teams/${teamId}/tasks`),
    enabled: !!teamId,
  });
}

export function useTaskWall(teamId: string) {
  return useQuery({
    queryKey: ['task-wall', teamId],
    queryFn: () => apiFetch<TaskWallResponse>(`/api/teams/${teamId}/task-wall`),
    enabled: !!teamId,
  });
}

export function useProjectTaskWall(projectId: string) {
  return useQuery({
    queryKey: ['project-task-wall', projectId],
    queryFn: () => apiFetch<TaskWallResponse>(`/api/projects/${projectId}/task-wall?include_completed=true&limit=50`),
    enabled: !!projectId,
  });
}

export function useTask(id: string) {
  return useQuery({
    queryKey: ['tasks', id],
    queryFn: () => apiFetch<APIResponse<Task>>(`/api/tasks/${id}`),
    enabled: !!id,
  });
}

export function useRunTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { team_id: string; title: string; description: string }) =>
      apiFetch<APIResponse<Task>>(`/api/teams/${data.team_id}/tasks/run`, {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ['teams', variables.team_id, 'tasks'] });
      void queryClient.invalidateQueries({ queryKey: ['project-task-wall'] });
      void queryClient.invalidateQueries({ queryKey: ['task-wall', variables.team_id] });
    },
  });
}
