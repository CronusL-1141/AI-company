import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Team, APIListResponse } from '../types';

export function useTeams() {
  return useQuery({
    queryKey: ['teams'],
    queryFn: () => apiFetch<APIListResponse<Team>>('/api/teams'),
  });
}
