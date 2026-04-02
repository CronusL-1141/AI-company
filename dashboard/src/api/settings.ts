import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';

export interface WakeConfig {
  interval: '10m' | '30m' | '1h' | 'off';
  prompt_template: string;
  autonomy_level: 'full' | 'consult' | 'readonly';
}

export function useWakeConfig() {
  return useQuery({
    queryKey: ['settings', 'wake-config'],
    queryFn: () => apiFetch<WakeConfig>('/api/settings/wake-config'),
  });
}

export function useUpdateWakeConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: WakeConfig) =>
      apiFetch<{ ok: boolean; config: WakeConfig }>('/api/settings/wake-config', {
        method: 'PUT',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['settings', 'wake-config'] });
    },
  });
}
