import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { APIListResponse, APIResponse } from '@/types';

// ─────────────────────────────────────────────────────────────────────────────
// 类型：对齐 docs/workflow-observability-design.md §2.1 / §2.2 的表结构。
// 观测层新表 workflow_runs / workflow_agents 是「不可变文件的可重建缓存」，
// 前端只读展示，不做任何写投影。
// ─────────────────────────────────────────────────────────────────────────────

// killed/failed 是真实存在的终态（本机实测约 10% 的 run 以此收尾），
// 后端 status 为自由字符串——保留 (string & {}) 兜底未知新状态不炸类型。
export type WorkflowStatus =
  | 'planned'
  | 'running'
  | 'completed'
  | 'interrupted'
  | 'killed'
  | 'failed'
  | (string & {});
export type WorkflowAgentState = 'queued' | 'running' | 'done' | (string & {});

export interface WorkflowPhase {
  index: number;
  title: string;
}

export interface WorkflowRun {
  id: string;
  wf_id: string;
  project_id?: string | null;
  team_id?: string | null;
  session_id?: string | null;
  cc_task_id?: string | null;
  name: string;
  status: WorkflowStatus;
  /** 数据面溯源：hook（仅骨架）/ file（文件对账）/ hook+file */
  source: string;
  phases: WorkflowPhase[];
  planned_agent_count: number;
  dynamic_nodes: number;
  agent_count: number;
  total_tokens: number;
  total_tool_calls: number;
  duration_ms?: number | null;
  summary?: string | null;
  result?: unknown;
  script_path?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at: string;
  updated_at: string;
  // ── Phase 2 live 观测列（运行期近似值，终态由 wf_<id>.json 文件值覆盖，design §10.1）──
  /** 运行期 token 估值 = Σ agent lastCtx（cached agent 记 0）；终态 UI 切回 total_tokens */
  live_tokens?: number | null;
  /** max(journal 与全部 agent-*.jsonl 的 mtime)；interrupted 判定依据 + UI「最后活动」 */
  last_activity_at?: string | null;
  /** journal.jsonl 已消费字节水位（服务端 tail 用，UI 仅透传不展示） */
  journal_offset?: number | null;
  /** wf_<id>.json 的 mtime_ns:size 指纹（reconcile 廉价跳过，UI 仅透传不展示） */
  source_fingerprint?: string | null;
}

export interface WorkflowAgent {
  id: string;
  run_id: string;
  wf_id: string;
  project_id?: string | null;
  cc_agent_id: string;
  os_agent_id?: string | null;
  label: string;
  phase_index?: number | null;
  phase_title?: string | null;
  model?: string | null;
  state: WorkflowAgentState;
  tokens: number;
  tool_calls: number;
  duration_ms?: number | null;
  last_tool_name?: string | null;
  last_tool_summary?: string | null;
  prompt_preview?: string | null;
  result_preview?: string | null;
  started_at?: string | null;
  queued_at?: string | null;
  created_at: string;
  updated_at: string;
  /** Phase 2：该 agent jsonl 的 mtime；泳道 running bar 右端锚点 */
  last_activity_at?: string | null;
}

export interface WorkflowFilters {
  status?: string;
  project_id?: string;
  limit?: number;
}

export interface ReconcileResult {
  ingested: number;
  updated: number;
}

// 兼容两种端点契约：APIListResponse<T>（{data,total,message}，对齐 events/teams）
// 或裸数组 T[]（frontendPlan 里的 apiFetch<WorkflowRun[]> 简写）。API 尚在并行开发，
// 两种形态都归一为数组，保证真实调用路径落地即可用。
function toList<T>(res: APIListResponse<T> | T[] | null | undefined): T[] {
  if (!res) return [];
  if (Array.isArray(res)) return res;
  return res.data ?? [];
}

// 详情端点契约为裸 WorkflowRun（design §4），但也兼容被 APIResponse 包裹的情况。
function unwrapRun(res: APIResponse<WorkflowRun> | WorkflowRun): WorkflowRun {
  if (res && typeof res === 'object' && 'data' in res && !('wf_id' in res)) {
    return (res as APIResponse<WorkflowRun>).data;
  }
  return res as WorkflowRun;
}

/**
 * 运行列表。含 running 的列表挂 refetchInterval≈15s（对齐 decisions.ts:26），
 * 兜底某次 completed 事件丢失时靠轮询 + 文件对账补齐；全 terminal 态则停轮询省流量。
 */
export function useWorkflows(filters?: WorkflowFilters) {
  const params = new URLSearchParams();
  if (filters?.status) params.set('status', filters.status);
  if (filters?.project_id) params.set('project_id', filters.project_id);
  if (filters?.limit) params.set('limit', String(filters.limit));
  const qs = params.toString();
  const url = `/api/workflows${qs ? `?${qs}` : ''}`;

  return useQuery({
    queryKey: ['workflows', filters ?? {}],
    queryFn: async (): Promise<WorkflowRun[]> => {
      try {
        const res = await apiFetch<APIListResponse<WorkflowRun> | WorkflowRun[]>(url);
        return toList(res);
      } catch {
        // API 并行开发期端点可能尚未上线：兜底空列表，让页面渲染空态而非硬崩。
        return [];
      }
    },
    refetchInterval: (query) =>
      (query.state.data ?? []).some((r) => r.status === 'running') ? 15000 : false,
  });
}

/** 运行详情。running 态挂轮询兜底完成检测（文件对账在服务端）。 */
export function useWorkflow(wfId: string) {
  return useQuery({
    queryKey: ['workflows', wfId],
    enabled: !!wfId,
    queryFn: async (): Promise<WorkflowRun> => {
      const res = await apiFetch<APIResponse<WorkflowRun> | WorkflowRun>(
        `/api/workflows/${encodeURIComponent(wfId)}`,
      );
      return unwrapRun(res);
    },
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 15000 : false),
  });
}

/**
 * 逐-agent 遥测（供详情表格 / 相位泳道）。
 * live=true（run 处于 running/interrupted）时挂 15s 轮询，对齐 useWorkflows/useWorkflow
 * 的既有节奏；终态停轮询省流量。不做本地秒级 ticker（design §10.8）。
 */
export function useWorkflowAgents(wfId: string, live = false) {
  return useQuery({
    queryKey: ['workflows', wfId, 'agents'],
    enabled: !!wfId,
    queryFn: async (): Promise<WorkflowAgent[]> => {
      try {
        const res = await apiFetch<APIListResponse<WorkflowAgent> | WorkflowAgent[]>(
          `/api/workflows/${encodeURIComponent(wfId)}/agents`,
        );
        return toList(res);
      } catch {
        return [];
      }
    },
    refetchInterval: live ? 15000 : false,
  });
}

/** 手动对账：把 OS 曾离线期间完成的运行的富数据拉进来（POST /reconcile）。 */
export function useReconcileWorkflows() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body?: { session_id?: string; project_dir?: string }) =>
      apiFetch<ReconcileResult>('/api/workflows/reconcile', {
        method: 'POST',
        body: JSON.stringify(body ?? {}),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['workflows'] });
    },
  });
}
