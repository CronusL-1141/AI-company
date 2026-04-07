import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Event, APIListResponse } from '../types';

export interface EventFilters {
  type?: string;
  source?: string;
  limit?: number;
  project_id?: string;
}

export function useEvents(filters?: EventFilters) {
  const params = new URLSearchParams();
  if (filters?.type) params.set('type', filters.type);
  if (filters?.source) params.set('source', filters.source);
  if (filters?.limit) params.set('limit', String(filters.limit));
  if (filters?.project_id) params.set('project_id', filters.project_id);
  const qs = params.toString();
  const url = `/api/events${qs ? `?${qs}` : ''}`;

  return useQuery({
    queryKey: ['events', filters],
    queryFn: () => apiFetch<APIListResponse<Event>>(url),
  });
}
