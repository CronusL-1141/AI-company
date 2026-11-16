import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { SyncNodes } from '@/lib/nodes';

/** The nodes whose rows this node holds (multi-host sync); role standalone when it does not sync. */
export function useSyncNodes() {
  return useQuery({
    queryKey: ['sync', 'nodes'],
    queryFn: () => apiFetch<SyncNodes>('/api/sync/nodes'),
    staleTime: 60_000,
  });
}
