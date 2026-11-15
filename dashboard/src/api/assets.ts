import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';

// Workflow asset backup (GET /api/assets/status). Read-only here: enrolling,
// running and answering the secret scan are the user's acts through the MCP tools.

export type AssetHost = 'claude' | 'codex';

export interface AssetPathRef {
  rel_path: string;
  category: string;
}

export interface AssetRootStatus {
  id: string;
  host: AssetHost;
  path: string;
  profile: string;
  status: 'enrolled' | 'ignored';
  this_machine: boolean;
  /** Why the last comparison stopped: the folder or the OS data folder is not there. */
  stopped: '' | 'folder_missing' | 'store_missing';
  categories: string[];
  enrolled_at: string;
  last_backup_at: string | null;
  backed_up: Record<string, number>;
  not_backed_up: Record<string, { total: number; items: AssetPathRef[] }>;
}

export interface AssetFinding extends AssetPathRef {
  finding_id: string;
  root_path: string;
  rule: string;
  line: number;
}

export type AssetWarning =
  | { code: 'storage_over_limit'; stored_bytes: number; limit_bytes: number }
  | { code: 'disk_low'; free_ratio: number };

export interface AssetBackupStatus {
  roots: AssetRootStatus[];
  pending_findings: { total: number; items: AssetFinding[] };
  stored_bytes: number;
  warnings: AssetWarning[];
  /** Why this machine cannot enroll a folder (non-macOS, no hardware id); empty when it can. */
  machine_refusal: string;
}

export function useAssetBackupStatus(host: AssetHost) {
  return useQuery({
    queryKey: ['assets', 'status', host],
    queryFn: () => apiFetch<{ success: boolean; data: AssetBackupStatus }>(`/api/assets/status?host=${host}`)
      .then((body) => body.data),
    refetchInterval: 60_000,
  });
}
