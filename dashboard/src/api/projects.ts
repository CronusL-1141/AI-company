import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Project, Phase, APIResponse, APIListResponse } from '@/types';

export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: () => apiFetch<APIListResponse<Project>>('/api/projects'),
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: ['projects', id],
    queryFn: () => apiFetch<APIResponse<Project>>(`/api/projects/${id}`),
    enabled: !!id,
  });
}

export function useProjectPhases(projectId: string) {
  return useQuery({
    queryKey: ['projects', projectId, 'phases'],
    queryFn: () =>
      apiFetch<APIListResponse<Phase>>(`/api/projects/${projectId}/phases`),
    enabled: !!projectId,
  });
}

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      name: string;
      root_path?: string;
      description?: string;
      config?: Record<string, unknown>;
    }) =>
      apiFetch<APIResponse<Project>>('/api/projects', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<APIResponse<null>>(`/api/projects/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export interface SummaryLeader {
  name: string;
  model: string;
  harness?: string | null;
  status: string;
  session_id: string;
  current_task: string;
  last_active_at: string | null;
  /** 15min 窗内有落盘（多会话并列展示时逐条标注） */
  live?: boolean;
  /** 主会话上下文水位（fleet 层 P2 观测，见 docs/fleet-layer-design.md §6）。
   *  探测不可用（DB 兜底路径）时为 null，代表"未知"而非 0。 */
  ctx_tokens?: number | null;
  ctx_window?: number | null;
  ctx_pct?: number | null;
  /** 可靠关联到该会话的 running 任务数；未知归属/统计失败为 null。 */
  in_flight_tasks?: number | null;
}

/** 已观察到的主会话；离线/空闲不等于宿主会话已经结束。 */
export interface SummarySession extends SummaryLeader {
  /** 有新鲜工作证据；activity_state 独立保留最近观测状态。 */
  live: boolean;
  activity_state: 'busy' | 'idle' | 'offline' | 'unknown';
  /** 取值与 services/project_sessions.py 一致；未识别的新取值前端显示为未知。 */
  status_source: 'db_observation' | 'cc_registry' | 'codex_rollout' | 'activity_only' | 'identity_only';
  /** 首次可靠记录时间，不代表宿主启动时间。 */
  started_at: string | null;
  in_flight_task_ids: string[];
}

export interface SummaryWorktree {
  path: string;
  branch: string | null;
  /** short HEAD sha */
  head: string;
  /** 是否有未提交/未跟踪变更 */
  dirty: boolean;
  /** 分支头是否已合入主分支；探测失败/无法判定时为 null */
  merged: boolean | null;
  locked: boolean;
  locked_reason?: string | null;
}

export interface ProjectSummary {
  status: 'active' | 'inactive';
  active_teams: number;
  pending_tasks: number;
  running_tasks: number;
  /** 与 sessions 同源的已记录主会话总数（封顶前），包含所有已观察宿主。 */
  session_count?: number | null;
  /** live 会话全部保留，其余按最近活跃补足，总数封顶 20；被截掉多少看 session_count。 */
  sessions?: SummarySession[];
  /** sessions 是否因封顶少于 session_count。 */
  sessions_truncated?: boolean;
  sessions_complete?: boolean;
  /** 项目任务墙中未能可靠归属到一个会话的 running 任务。 */
  unattributed_running_tasks?: number | null;
  last_activity_at?: string | null;
  /** 按 project_id 直出的最新 Leader（不经 team 链——Leader 行可能寄生在跨项目 workflow 队） */
  leader?: SummaryLeader | null;
  /** 当前有新鲜工作证据的主会话，包含全部已观察宿主；没有时为空数组。 */
  leaders?: SummaryLeader[] | null;
  /** 该项目下从属 worktree（按需扫描，不含主 checkout 本身） */
  worktrees?: SummaryWorktree[] | null;
  top_tasks: { id?: string; title: string; priority: string; horizon?: string }[];
}

export function useProjectSummary(projectId: string) {
  return useQuery({
    queryKey: ['projects', projectId, 'summary'],
    queryFn: () => apiFetch<ProjectSummary>(`/api/projects/${projectId}/summary`),
    enabled: !!projectId,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}
