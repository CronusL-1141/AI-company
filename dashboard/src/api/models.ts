import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '@/api/client';

/** 模型治理 — 可用模型自动拉取（文件真相源：本机 transcript 实际出现过的模型）。 */

export interface AvailableModel {
  model: string;
  file_count: number;
  last_seen_ts: number;
  /** 层级别名（如 "opus"），不进默认下拉主列表 */
  alias: boolean;
}

export function useAvailableModels() {
  return useQuery({
    queryKey: ['models', 'available'],
    queryFn: () => apiFetch<{ data: AvailableModel[] }>('/api/models/available'),
    staleTime: 60_000,
  });
}

export function useDefaultModel() {
  return useQuery({
    queryKey: ['models', 'default'],
    queryFn: () => apiFetch<{ data: { model: string } }>('/api/models/default'),
  });
}

export function useSetDefaultModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (model: string) =>
      apiFetch<{ success: boolean; data: { ok: boolean; error?: string } }>(
        '/api/models/default',
        { method: 'PUT', body: JSON.stringify({ model }) },
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['models'] }),
  });
}
