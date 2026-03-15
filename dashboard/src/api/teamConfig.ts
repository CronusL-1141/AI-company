import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';

export interface PermanentMember {
  name: string;
  role: string;
  model: string;
  enabled: boolean;
}

export interface TeamDefaults {
  auto_create_team: boolean;
  team_name_prefix: string;
  permanent_members: PermanentMember[];
}

interface ApiResponse<T> {
  success: boolean;
  data: T;
}

export function useTeamDefaults() {
  return useQuery({
    queryKey: ['system', 'team-defaults'],
    queryFn: async () => {
      const res = await apiFetch<ApiResponse<TeamDefaults>>(
        '/api/config/team-defaults',
      );
      return res.data;
    },
    refetchInterval: 30_000,
  });
}

export function useUpdateTeamDefaults() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: TeamDefaults) =>
      apiFetch<ApiResponse<TeamDefaults>>('/api/config/team-defaults', {
        method: 'PUT',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['system', 'team-defaults'] });
    },
  });
}

export function useAddPermanentMember() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (member: PermanentMember) =>
      apiFetch<ApiResponse<PermanentMember>>('/api/config/team-defaults/members', {
        method: 'POST',
        body: JSON.stringify(member),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['system', 'team-defaults'] });
    },
  });
}

export function useRemovePermanentMember() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch<ApiResponse<null>>(`/api/config/team-defaults/members/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['system', 'team-defaults'] });
    },
  });
}

export function useTogglePermanentMember() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      apiFetch<ApiResponse<PermanentMember>>(
        `/api/config/team-defaults/members/${encodeURIComponent(name)}`,
        {
          method: 'PATCH',
          body: JSON.stringify({ enabled }),
        },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['system', 'team-defaults'] });
    },
  });
}
