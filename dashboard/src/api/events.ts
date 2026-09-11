import { useQueries, useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Event, APIListResponse } from '../types';

export interface EventFilters {
  type?: string;
  type_prefix?: string;
  source?: string;
  limit?: number;
  project_id?: string;
}

function eventsUrl(filters?: EventFilters) {
  const params = new URLSearchParams();
  if (filters?.type_prefix) params.set('type_prefix', filters.type_prefix);
  else if (filters?.type) params.set('type', filters.type);
  if (filters?.source) params.set('source', filters.source);
  if (filters?.limit) params.set('limit', String(filters.limit));
  if (filters?.project_id) params.set('project_id', filters.project_id);
  const qs = params.toString();
  return `/api/events${qs ? `?${qs}` : ''}`;
}

export function useEvents(filters?: EventFilters) {
  return useQuery({
    queryKey: ['events', filters],
    queryFn: () => apiFetch<APIListResponse<Event>>(eventsUrl(filters)),
  });
}

export function useFailureEvents(projectId?: string) {
  const types = ['task.failure_analyzed', 'task.failed', 'failure_analysis', 'task_failed'];
  const queries = useQueries({ queries: types.map((type) => {
    const filters = { type, project_id: projectId, limit: 100 };
    return { queryKey: ['events', filters],
      queryFn: () => apiFetch<APIListResponse<Event>>(eventsUrl(filters)) };
  }) });
  return {
    data: [...new Map(queries.flatMap((q) => q.data?.data ?? []).map((event) => [event.id, event])).values()],
    isLoading: queries.some((q) => q.isLoading),
    error: queries.find((q) => q.error)?.error,
  };
}
