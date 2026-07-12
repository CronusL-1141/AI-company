import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '@/api/client';

/** 后端版本真相源：/api/health 直出 aiteam.__version__（勿在前端写死版本号）。 */
export function useApiVersion() {
  return useQuery({
    queryKey: ['health', 'version'],
    queryFn: () => apiFetch<{ status: string; version: string }>('/api/health'),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
}
