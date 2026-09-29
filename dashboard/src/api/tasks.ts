import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type {
  Task, TaskWallResponse, TaskWallDigest, CompletedTaskPage, APIResponse, APIListResponse,
} from '../types';

// The page shows every open task (it folds long tails itself); limit pages the
// pending ones only, so this is a ceiling, not a page size.
const OPEN_TASK_CEILING = 500;
const COMPLETED_PAGE = 50;

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
    // Open tasks as card rows plus the digest; a task loads in full when opened
    // (useTask), completed tasks when their section opens (useCompletedTasks).
    queryFn: () => apiFetch<TaskWallResponse>(
      `/api/projects/${projectId}/task-wall?limit=${OPEN_TASK_CEILING}&fields=card`,
    ),
    enabled: !!projectId,
  });
}

/**
 * Completed tasks as short rows, newest first, 50 per page; nothing is fetched until enabled.
 * Its key is outside 'project-task-wall' on purpose: a task event refetches the wall
 * only, and the section refreshes itself when the wall's completed count moves.
 */
export function useCompletedTasks(projectId: string, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: completedTasksKey(projectId),
    queryFn: ({ pageParam }) => apiFetch<CompletedTaskPage>(
      `/api/projects/${projectId}/task-wall?status=completed&include_completed=true&limit=0`
      + `&completed_limit=${COMPLETED_PAGE}&completed_offset=${pageParam}`,
    ),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => (last.completed_has_more ? pages.length * COMPLETED_PAGE : undefined),
    enabled: !!projectId && enabled,
  });
}

export function completedTasksKey(projectId: string) {
  return ['project-task-completed', projectId];
}

/**
 * The digest alone (a few KB): counts for the overview's project cards. Keyed under
 * 'projects' like the full wall these cards read before, so task events do not
 * refetch one digest per card; project events and focus do.
 */
export function useProjectTaskDigest(projectId: string) {
  return useQuery({
    queryKey: ['projects', projectId, 'task-digest'],
    queryFn: () => apiFetch<TaskWallDigest>(`/api/projects/${projectId}/task-wall/digest`),
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
