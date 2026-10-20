import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { APIResponse } from '@/types';

export type ExternalAssociationStatus = 'unverified' | 'accepted' | 'rejected' | 'quarantined';
export type ExternalAssociationReason = 'project_not_found' | 'task_not_found' | 'task_project_mismatch';
export type ExternalDiagnostic =
  | 'missing_usage' | 'invalid_event' | 'invalid_usage' | 'oversized_event' | 'spawn_failed'
  | 'signal_exit' | 'adapter_interrupted' | 'stream_incomplete' | 'event_failure' | 'error_event'
  | 'missing_thread_id' | 'missing_turn_id' | 'api_unavailable' | 'association_rejected'
  | 'association_quarantined' | 'parent_lost' | 'usage_zero_unverified' | 'process_cleanup_unverified';

export interface ExternalReceipt {
  receipt_id: string;
  thread_id: string | null;
  turn_id: string | null;
  counting_level: 'turn_aggregate' | 'thread_aggregate' | 'unknown';
  usage_evidence: 'reported' | 'zero_unverified';
  usage: Partial<Record<'input_tokens' | 'cached_input_tokens' | 'cache_write_input_tokens' | 'output_tokens' | 'reasoning_output_tokens' | 'total_tokens', number | null>> | null;
  provider: string | null;
  model: string | null;
  service_tier: string | null;
  account_key: string | null;
  metadata_source: 'explicit_observation' | 'unknown';
  observed_at: string;
}
export interface ExternalInvocation {
  invocation_id: string;
  project_id: string | null;
  task_id: string | null;
  requested_project_id: string | null;
  requested_task_id: string | null;
  association_status: ExternalAssociationStatus;
  association_reason: ExternalAssociationReason | null;
  caller_harness: 'codex' | 'claude-code' | 'other' | 'unknown';
  caller_session_id: string | null;
  callee_harness: 'codex';
  source_namespace: string;
  ephemeral: boolean;
  started_at: string;
  thread_id: string | null;
  status: 'running' | 'completed' | 'failed' | 'interrupted';
  exit_code: number | null;
  finished_at: string | null;
  revision: number;
  receipts: Record<string, ExternalReceipt>;
  receipts_count: number;
  receipts_offset: number;
  receipts_limit: number;
  receipts_truncated: boolean;
  usage_observed: boolean;
  diagnostics: ExternalDiagnostic[];
}
export type ReconciliationBucket =
  | 'unregistered_threads' | 'finished_without_usage' | 'tokens_without_complete_pricing'
  | 'external_without_journal' | 'registered_without_evidence' | 'running_invocations'
  | 'journal_project_unknown' | 'quarantined_invocations' | 'pricing_metadata_unverified';
export interface ReconciliationItem {
  thread_id: string | null;
  source_namespace: string | null;
  source: 'journal' | 'external_exec' | null;
  invocation_id: string | null;
  agent_id: string | null;
  status: ExternalInvocation['status'] | null;
  ephemeral: boolean | null;
  registered_globally: boolean | null;
  reasons: string[];
  association_reason: ExternalAssociationReason | null;
  requested_project_id: string | null;
  requested_task_id: string | null;
}
export interface ExternalReconciliation {
  observed_at: string;
  scope: {
    project_id: string | null; source_namespace: string | null; since: string | null; until: string | null;
    journal_project_attribution: 'unknown'; history: 'saved_evidence_only';
    registration_existence: 'global_all_saved_evidence'; agent_source_namespace: 'unknown';
    time_basis: Record<'journal' | 'agents' | 'invocations', string>;
  };
  counts: Record<'journal_observations' | 'journal_threads' | 'registered_codex_agents' | 'external_invocations' | 'discovered_threads' | 'overlap_threads', number>;
  buckets: Record<ReconciliationBucket, ReconciliationItem[]>;
  bucket_counts: Record<ReconciliationBucket, number>;
  bucket_truncated: Record<ReconciliationBucket, boolean>;
  bucket_offset: number;
  bucket_limit: number;
  sample_limit: number;
  summary: boolean;
  truncated: Record<'journal' | 'agents' | 'invocations' | 'cursors', boolean>;
  journal_status: { cursor_count: number; incomplete_cursors: number; unknown_observation_times_in_sample: boolean };
  statements: string[];
}
/** Evidence coverage is usage_observed executions / executions on this saved page.
 * A missing receipt is unknown, never zero usage; this is not whole-account coverage.
 */
export type ExternalInvocationRow = Omit<ExternalInvocation, 'receipts'>;
interface ExternalList { items: ExternalInvocationRow[]; total: number; limit: number; offset: number; truncated: boolean }
export interface CodexExecLaunchObservation {
  launch_id: string;
  project_id: string | null;
  caller_session_id: string;
  tool_use_id: string;
  command_index: number;
  exec_mode: 'new' | 'resume' | 'fork' | 'review' | 'unknown';
  requested_service_tier: 'standard' | 'default' | 'fast' | 'priority' | 'flex' | 'batch' | null;
  requested_model: string | null;
  requested_reasoning_effort: string | null;
  requested_metadata_source: 'cc_bash_command';
  state: 'planned' | 'unbound' | 'bound' | 'failed';
  binding_reason: 'awaiting_completion' | 'complex_shell' | 'background_or_redirect' | 'not_json'
    | 'ephemeral' | 'missing_thread_started' | 'native_thread_unverified' | 'tool_failed'
    | 'multiple_launches' | 'missing_pre_event' | 'conflicting_thread' | 'dynamic_shell' | 'bound'
    | 'output_pending' | 'unsafe_output_file' | 'invalid_output_header' | 'stale_output_file' | 'output_path_conflict';
  output_path: string | null;
  codex_home: string | null;
  background: boolean;
  execution_failed: boolean;
  pre_seen: boolean;
  source_namespace: string | null;
  thread_id: string | null;
  turn_ids: string[];
  started_at: string;
  finished_at: string | null;
}
export interface CodexLaunchObservationList {
  items: CodexExecLaunchObservation[];
  total: number;
  limit: number;
  offset: number;
  truncated: boolean;
}
export function useCodexLaunchObservations(offset: number, enabled: boolean) {
  return useQuery({ queryKey: ['usage', 'codex-launch-observations', offset], enabled,
    queryFn: () => apiFetch<APIResponse<CodexLaunchObservationList>>(
      `/api/external-execs/launch-observations?limit=20&offset=${offset}`,
    ) });
}
export function useExternalReconciliation(since: string | null, bucketOffset: number) {
  const query = new URLSearchParams({ limit: '500', bucket_offset: String(bucketOffset), bucket_limit: '20', summary: 'false' });
  if (since) query.set('since', since);
  return useQuery({ queryKey: ['usage', 'external-reconciliation', since, bucketOffset],
    queryFn: () => apiFetch<APIResponse<ExternalReconciliation>>(`/api/external-execs/reconciliation?${query}`) });
}
export function useExternalInvocations(offset: number) {
  return useQuery({ queryKey: ['usage', 'external-execs', offset],
    queryFn: () => apiFetch<APIResponse<ExternalList>>(`/api/external-execs?limit=20&offset=${offset}`) });
}
export function useExternalInvocation(id: string, enabled: boolean, receiptOffset: number) {
  return useQuery({ queryKey: ['usage', 'external-exec', id, receiptOffset], enabled,
    queryFn: () => apiFetch<APIResponse<ExternalInvocation>>(`/api/external-execs/${encodeURIComponent(id)}?receipt_offset=${receiptOffset}&receipt_limit=100`) });
}
