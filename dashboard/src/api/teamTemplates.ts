import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';

export interface TemplateMember {
  name: string;
  role: string;
}

export interface TeamTemplate {
  id: string;
  name: string;
  description: string;
  members: TemplateMember[];
}

interface ApiResponse<T> {
  success: boolean;
  data: T;
}

export function useTeamTemplates() {
  return useQuery({
    queryKey: ['system', 'team-templates'],
    queryFn: async () => {
      const res = await apiFetch<ApiResponse<TeamTemplate[]>>(
        '/api/config/team-templates',
      );
      return res.data;
    },
  });
}
