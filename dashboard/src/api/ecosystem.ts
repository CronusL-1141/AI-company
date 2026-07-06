import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';

// 与后端 /api/ecosystem/profiles 返回格式对齐
export interface EcosystemRepoProfile {
  id: string;
  repo_full_name: string;
  name: string;
  owner: string;
  description: string | null;
  stars: number;
  language: string | null;
  topics: string[];
  homepage: string | null;
  last_commit_at: string | null;
  needs_deep_review: boolean;
  /** @deprecated v1.6.1: relevance_category 启发式分类已弃用，后端不再返回 */
  relevance_category?: string | null;
  /** @deprecated v1.6.1: relevance_score 硬编码评分无参考价值，后端不再返回 */
  relevance_score?: number;
  one_line_summary: string | null;
  last_scanned_at: string;
  first_seen_at: string;
  // Stage B 扩展字段（v2 API 返回时填充）
  pushed_at?: string | null;
  is_archived?: boolean;
  scan_run_id?: string | null;
  description_excerpt?: string;
  // v1.5.0-A 扩展字段（前端 stage 徽章 / failed 提示 / 活跃集 tab 依赖）
  shallow_summary?: string;
  last_shallow_refreshed_at?: string | null;
  is_deleted?: boolean;
  is_private_now?: boolean;
  last_fetch_error?: string;
  fetch_failure_count?: number;
  /** @deprecated v1.6.0 P1.A: 使用 last_active_status 替代（'active' / 'archived' / 'manual_archived' / 'pinned'） */
  is_active?: boolean;
  /** @deprecated v1.6.0 P1.A: 排名机制已废弃，使用 last_active_status + manual_status 表达活跃度 */
  active_rank?: number | null;
  // v1.5.1：透出渐进漏斗 stage 状态（取自 latest deep_review，无 review = "queued"）
  stage_status?: string | null;
  // v1.5.1：研究次数（关联的 deep_review 行数，0 = 未深扫）
  research_count?: number;
  // v1.6.0：last_active_status — 'active' / 'archived' / 'manual_archived' / 'pinned' / null
  last_active_status?: string | null;
  // v1.6.1 多源：sources 列表 + primary_source
  sources?: { kind: string; id: string; url: string; [k: string]: unknown }[];
  primary_source?: string;
  canonical_id?: string | null;
  source_kind?: string;
}

// v1.5.0 漏斗 stage 状态
export const ECOSYSTEM_STAGE_STATUSES = [
  'queued',
  'shallow_done',
  'shallow_failed',
  'architecture_done',
  'architecture_failed',
  'debated',
  'debated_failed',
  'referenced',
  'integrated',
] as const;

export type EcosystemStageStatus = (typeof ECOSYSTEM_STAGE_STATUSES)[number];

/** stage 中文标签 */
export const STAGE_STATUS_LABELS: Record<EcosystemStageStatus, string> = {
  queued: '待浅扫',
  shallow_done: '浅扫完成',
  shallow_failed: '浅扫失败',
  architecture_done: '架构已分析',
  architecture_failed: '架构失败',
  debated: '已辩论',
  debated_failed: '辩论失败',
  referenced: '✓ 参考',
  integrated: '★ 已集成',
};

/** stage 颜色（与设计稿 §8.1 对齐：灰/蓝/黄/橙/绿/紫/红） */
export const STAGE_STATUS_TONE: Record<
  EcosystemStageStatus,
  'gray' | 'blue' | 'yellow' | 'orange' | 'green' | 'purple' | 'red'
> = {
  queued: 'gray',
  shallow_done: 'blue',
  shallow_failed: 'red',
  architecture_done: 'yellow',
  architecture_failed: 'red',
  debated: 'orange',
  debated_failed: 'red',
  referenced: 'green',
  integrated: 'purple',
};

/** stage 徽章 className（Tailwind 同构色） */
export function stageBadgeClass(stage: EcosystemStageStatus | string): string {
  const tone = STAGE_STATUS_TONE[stage as EcosystemStageStatus] ?? 'gray';
  switch (tone) {
    case 'gray':
      return 'bg-muted text-muted-foreground border-border';
    case 'blue':
      return 'bg-blue-500/10 text-blue-700 dark:text-blue-300 border-blue-500/30';
    case 'yellow':
      return 'bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/30';
    case 'orange':
      return 'bg-orange-500/10 text-orange-700 dark:text-orange-300 border-orange-500/30';
    case 'green':
      return 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30';
    case 'purple':
      return 'bg-purple-500/10 text-purple-700 dark:text-purple-300 border-purple-500/30';
    case 'red':
      return 'bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/30';
    default:
      return 'bg-muted text-muted-foreground border-border';
  }
}

export interface EcosystemFacetCounts {
  category: Record<string, number>;
  language: Record<string, number>;
  archived: Record<string, number>; // {"true": n, "false": n}
  // v1.5.1：渐进漏斗 stage 分布（不被 limit 截断的全量统计）
  // 例如：{"queued": 162, "shallow_done": 100, "debated": 3}
  stage?: Record<string, number>;
  // v1.6.0 SST: GitHub topics 维度全量统计（取代基于启发式的 category）
  // 例如：{"mcp": 120, "claude-code": 80, "ai": 75, ...}
  topics?: Record<string, number>;
}

export interface EcosystemProfilesResponse {
  profiles: EcosystemRepoProfile[];
  total: number;
  limit?: number;
  offset?: number;
  has_more?: boolean;
  facet_counts?: EcosystemFacetCounts;
}

export interface EcosystemFilters {
  keyword?: string;
  topic?: string;
  /** @deprecated v1.6.0: relevance_category 启发式分类已废弃，UI 改用 topics 多选筛选 */
  category?: string;
  /** v1.6.0: GitHub topics 多选筛选（客户端 filter；与 profile.topics 求交集） */
  topics?: string[];
  minStars?: number;
  maxStars?: number;
  needsDeepReview?: boolean | null;
  limit?: number;
  offset?: number;
  facetCounts?: boolean;
  // v1.5.0-E 新增：活跃/全量/已删除 tab + stage 维度筛选
  isActive?: boolean | null;
  isDeleted?: boolean | null;
  stageStatus?: string; // 多个用逗号分隔
}

/**
 * 列表查询：检索生态仓档案。
 * 对接 GET /api/ecosystem/profiles
 */
export function useEcosystemProfiles(filters: EcosystemFilters = {}) {
  const {
    keyword = '',
    topic = '',
    category = '',
    minStars = 0,
    maxStars = 0,
    needsDeepReview = null,
    limit = 100,
    offset = 0,
    facetCounts = false,
    isActive = null,
    isDeleted = null,
    stageStatus = '',
  } = filters;

  const params = new URLSearchParams();
  if (keyword) params.set('keyword', keyword);
  if (topic) params.set('topic', topic);
  if (category) params.set('category', category);
  if (minStars > 0) params.set('min_stars', String(minStars));
  if (maxStars > 0) params.set('max_stars', String(maxStars));
  if (needsDeepReview !== null) params.set('needs_deep_review', String(needsDeepReview));
  params.set('limit', String(limit));
  if (offset > 0) params.set('offset', String(offset));
  if (facetCounts) params.set('facet_counts', 'true');
  if (isActive !== null) params.set('is_active', String(isActive));
  if (isDeleted !== null) params.set('is_deleted', String(isDeleted));
  if (stageStatus) params.set('stage_status', stageStatus);

  return useQuery({
    queryKey: ['ecosystem', 'profiles', filters],
    queryFn: () => apiFetch<EcosystemProfilesResponse>(`/api/ecosystem/profiles?${params.toString()}`),
  });
}

/**
 * 单仓档案 hook（基础信息）— 用于不需要深度档案的场景。
 * 通过 /profiles 列表回退查找（保持向后兼容）。
 */
export function useEcosystemRepoDetail(repoId: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'repo', repoId],
    queryFn: async (): Promise<EcosystemRepoProfile | null> => {
      if (!repoId) return null;
      const data = await apiFetch<EcosystemProfilesResponse>(`/api/ecosystem/profiles?limit=100`);
      return data.profiles.find((p) => p.id === repoId) ?? null;
    },
    enabled: !!repoId,
  });
}

// ============================================================
// v2 API: 单仓全息详情 (BUG-023 修复)
// ============================================================

/** 能力 / 成熟度 / 风险 标签 */
export interface EcosystemTag {
  tag_id: string;
  name: string;
  category: string; // capability / maturity / risk / ...
  aliases: string[];
  description: string | null;
  confidence: number; // 0-1
  source: string; // github_topic / auto_rule / llm / manual
  agent_id: string | null;
  created_at: string;
}

/** 深扫记录 */
export interface EcosystemDeepReview {
  id: string;
  repo_id: string;
  status: string; // stage_status 的派生只读视图：queued / completed / failed（'running' 仅历史行）
  agent_id: string | null;
  summary_md: string;
  architecture_md: string;
  demo_result: string | null;
  demo_log_excerpt: string | null;
  risks_md: string;
  learnings_md: string;
  integration_recommendation: string | null; // adopt / experiment / hold / avoid
  report_id: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_seconds?: number;
  // v1.5.0-A 字段（前端 timeline 依赖）
  stage_status?: EcosystemStageStatus | string;
  integration_md?: string;
  shallow_completed_at?: string | null;
  architecture_completed_at?: string | null;
  debated_at?: string | null;
  stage3_completed_at?: string | null;
  debate_meeting_id?: string | null;
  integration_task_id?: string | null;
}

/** 仓与仓的关联关系 */
export interface EcosystemRelation {
  id: string;
  source_repo_id: string;
  target_repo_id: string;
  source_repo_full_name?: string;
  target_repo_full_name?: string;
  relation_type: string; // depends_on / inspired_by / fork_of / replaces / ...
  confidence: number;
  evidence: string | null;
  created_at: string;
}

/** 扫描运行记录（与后端 /api/ecosystem/scan-runs 返回对齐） */
export interface EcosystemScanRun {
  id: string;
  agent_id: string | null;
  /** 后端原始字段（incremental / full / topic / trending 等） */
  strategy: string;
  /** 前端派生：等同 strategy（向后兼容 UI 字段名） */
  scan_type: string;
  started_at: string;
  completed_at: string | null;
  /** 前端派生：completed_at != null → 'completed'，否则 'running'；errors 非空 → 'failed' */
  status: 'completed' | 'running' | 'failed' | string;
  duration_seconds?: number | null;
  repos_added: number;
  repos_updated: number;
  repos_skipped?: number;
  /** v1.6.1 Phase 2: repos with actual metadata changes (topics/stars/desc/lang) */
  metadata_changed_count?: number;
  errors?: string[];
  triggered_by?: string;
  notes: string | null;
}

/** 后端 /api/ecosystem/scan-runs 原始响应（字段在 hook 中映射） */
interface ScanRunsApiResponse {
  runs: Array<{
    id: string;
    agent_id: string | null;
    strategy: string;
    started_at: string;
    completed_at: string | null;
    duration_seconds?: number | null;
    repos_added: number;
    repos_updated: number;
    repos_skipped?: number;
    metadata_changed_count?: number;
    errors?: string[];
    triggered_by?: string;
    notes: string | null;
  }>;
  total: number;
}

/** v2 全息响应 — 与后端 EcosystemRepoFullResponse 对齐 */
export interface EcosystemRepoFullResponse {
  profile: EcosystemRepoProfile;
  tags: EcosystemTag[];
  deep_reviews: EcosystemDeepReview[];
  relations_from: EcosystemRelation[];
  relations_to: EcosystemRelation[];
  scan_run: EcosystemScanRun | null;
}

/** 深扫列表响应 — 用于统计真实 completed/running/failed 数 */
export interface EcosystemDeepReviewListResponse {
  reviews: EcosystemDeepReview[];
  total: number;
}

/**
 * 列出深扫记录，可按 status 过滤（queued / completed / failed；'running' 仅历史行）。
 * 用于 StatsBar 计算真实"已深扫"数量（语义 = DeepReview.status='completed' 的行数；
 * D5 收敛后 status 为 stage_status 的派生只读视图，'completed' 等价 stage 已达
 * shallow_done 及以上，口径自动正确化）。
 *
 * 注意：profile.needs_deep_review 字段语义是"是否需要被深扫"，false 不等于"已完成深扫"。
 * 必须用本接口拉真实 DeepReview 行为准。
 */
/**
 * GET /api/ecosystem/scan-runs?limit=N — 最近 N 次批次扫描记录（生态档案级）
 * 用于 EcosystemListPage 顶部展示"最近批次扫描"概览。
 * 返回按 started_at 倒序，最近一次 = list[0]。
 */
export function useEcosystemRecentScanRuns(limit: number = 5) {
  return useQuery({
    queryKey: ['ecosystem', 'scan-runs', limit],
    queryFn: async (): Promise<{ data: EcosystemScanRun[]; total: number }> => {
      const params = new URLSearchParams();
      params.set('limit', String(limit));
      const body = await apiFetch<ScanRunsApiResponse>(
        `/api/ecosystem/scan-runs?${params.toString()}`,
      );
      // 后端返回 {runs, total}，前端 hook 历来期望 {data, total}。
      // 同时派生 scan_type（= strategy）与 status（completed_at + errors 推断）。
      const data: EcosystemScanRun[] = body.runs.map((r) => {
        const hasErrors = Array.isArray(r.errors) && r.errors.length > 0;
        const status: EcosystemScanRun['status'] = r.completed_at
          ? hasErrors
            ? 'failed'
            : 'completed'
          : 'running';
        return {
          ...r,
          scan_type: r.strategy,
          status,
        };
      });
      return { data, total: body.total };
    },
    staleTime: 30_000,
  });
}

export function useEcosystemDeepReviews(status: string = '') {
  return useQuery({
    queryKey: ['ecosystem', 'deep_reviews', status],
    queryFn: () => {
      const params = new URLSearchParams();
      if (status) params.set('status', status);
      params.set('limit', '100'); // 后端上限 100；当前数据量 < 100 充足
      return apiFetch<EcosystemDeepReviewListResponse>(
        `/api/ecosystem/deep_reviews?${params.toString()}`,
      );
    },
    staleTime: 30_000,
  });
}

/**
 * v2: GET /api/ecosystem/profiles/{repo_full_name:path}/full — 全息详情
 *
 * 接受 UUID 或 repo_full_name。若传 UUID，会先在缓存的列表里查找对应 full_name。
 * 失败时返回 null（不抛错），上层降级展示基础信息。
 */
export function useEcosystemRepoFull(repoIdOrName: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'repo-full', repoIdOrName],
    queryFn: async (): Promise<EcosystemRepoFullResponse | null> => {
      if (!repoIdOrName) return null;

      // 判定是 UUID 还是 owner/name
      let repoFullName = repoIdOrName;
      const looksLikeUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
        repoIdOrName,
      );
      if (looksLikeUuid) {
        // 后端 limit 上限 200，分页拉取所有页直到命中
        const pageSize = 200;
        let offset = 0;
        let hit: EcosystemRepoProfile | undefined;
        // 防御性兜底：最多 10 页（2000 仓），避免循环
        for (let page = 0; page < 10; page++) {
          const list = await apiFetch<EcosystemProfilesResponse & { offset?: number }>(
            `/api/ecosystem/profiles?limit=${pageSize}&offset=${offset}`,
          );
          hit = list.profiles.find((p) => p.id === repoIdOrName);
          if (hit) break;
          if (list.profiles.length < pageSize) break; // 末页
          offset += pageSize;
        }
        if (!hit) return null;
        repoFullName = hit.repo_full_name;
      }

      // path 参数包含斜杠：单段 encode 即可（path-converter 接受 /）
      const encoded = repoFullName
        .split('/')
        .map((seg) => encodeURIComponent(seg))
        .join('/');
      try {
        return await apiFetch<EcosystemRepoFullResponse>(
          `/api/ecosystem/profiles/${encoded}/full`,
        );
      } catch {
        return null;
      }
    },
    enabled: !!repoIdOrName,
    retry: false,
  });
}

/** 评审 status 中文映射 — 通用语义（v1.5.2: "深扫中" → "评审中"，因为 status 不区分浅/深 stage） */
export const DEEP_REVIEW_STATUS_LABELS: Record<string, string> = {
  pending: '待评审',
  running: '评审中',
  completed: '已完成',
  failed: '失败',
  skipped: '已跳过',
};

/** 集成建议中文映射 */
export const INTEGRATION_RECOMMENDATION_LABELS: Record<string, string> = {
  adopt: '采纳',
  experiment: '试验',
  hold: '观望',
  avoid: '回避',
};

/** 关联关系类型中文映射 */
export const RELATION_TYPE_LABELS: Record<string, string> = {
  depends_on: '依赖',
  inspired_by: '受启发',
  fork_of: 'Fork 自',
  replaces: '替代',
  similar_to: '相似',
  extends: '扩展',
  uses: '使用',
};

/**
 * 类别选项 — 与后端 EcosystemRepoProfile.relevance_category 对齐。
 */
export const RELEVANCE_CATEGORIES = [
  'agent-framework',
  'mcp-server',
  'memory-system',
  'skill-system',
  'tooling',
] as const;

export type RelevanceCategory = (typeof RELEVANCE_CATEGORIES)[number];

/**
 * v1.6.0: Topic badge 动态颜色调色板 (StatsBar + RepoCard 共享).
 * 按位置 idx % length 循环, top N 排名变化时颜色自动跟随位置.
 * 用低饱和度 (secondary variant 基础 + 柔和边框/文字色), 保持简洁不刺眼.
 */
export const TOPIC_COLOR_PALETTE: readonly string[] = [
  'border-blue-500/30 text-blue-700 dark:text-blue-300',
  'border-emerald-500/30 text-emerald-700 dark:text-emerald-300',
  'border-amber-500/30 text-amber-700 dark:text-amber-300',
  'border-purple-500/30 text-purple-700 dark:text-purple-300',
  'border-rose-500/30 text-rose-700 dark:text-rose-300',
  'border-cyan-500/30 text-cyan-700 dark:text-cyan-300',
  'border-orange-500/30 text-orange-700 dark:text-orange-300',
  'border-pink-500/30 text-pink-700 dark:text-pink-300',
] as const;

/**
 * @deprecated v1.6.0 SST: relevance_category 启发式分类已废弃，UI 改用真实 GitHub topics。
 * 此映射保留仅作老数据回显兼容。新代码不应使用。
 */
export const CATEGORY_LABELS: Record<string, string> = {
  'agent-framework': 'Agent 框架',
  'mcp-server': 'MCP 服务器',
  'memory-system': '记忆系统',
  'skill-system': '技能系统',
  tooling: '开发工具',
};

// ============================================================
// v1.5.0-E: Project Settings (决策 12.1)
// ============================================================

/** 项目级 ecosystem 配置 — 与后端 EcosystemProjectSettings 对齐 */
export interface EcosystemProjectSettings {
  project_id: string;
  min_stars: number;
  top_n: number;
  refresh_interval_days: number;
  auto_shallow_on_archive: boolean;
  focus_topics: string[];
  focus_languages: string[];
  shallow_concurrency: number;
  deep_concurrency: number;
  /** v1.6.1 Phase 2: 每次扫描最多允许新增的仓数（超过触发告警），从 scan_profile 迁移 */
  alert_max_new_per_scan: number;
  created_at: string | null;
  updated_at: string | null;
}

/** 用 PUT 提交时的入参（不带 timestamps） */
export type EcosystemProjectSettingsInput = Omit<
  EcosystemProjectSettings,
  'project_id' | 'created_at' | 'updated_at'
>;

/** GET /api/ecosystem/projects/{project_id}/settings */
export function useEcosystemProjectSettings(projectId: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'project-settings', projectId],
    queryFn: () =>
      apiFetch<EcosystemProjectSettings>(
        `/api/ecosystem/projects/${projectId}/settings`,
      ),
    enabled: !!projectId,
  });
}

/** PUT /api/ecosystem/projects/{project_id}/settings */
export function useUpdateProjectSettings(projectId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (input: EcosystemProjectSettingsInput) => {
      if (!projectId) throw new Error('projectId required');
      return await apiFetch<EcosystemProjectSettings>(
        `/api/ecosystem/projects/${projectId}/settings`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(input),
        },
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'project-settings', projectId] });
    },
  });
}

// ============================================================
// v1.5.0-E: Failed repo retry
// ============================================================

/** POST /api/ecosystem/profiles/{repo_id}/retry — 立即重试失败的仓 */
export function useRetryFailedRepo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (repoId: string) =>
      apiFetch<{ success: boolean; repo_full_name: string; next_action: string }>(
        `/api/ecosystem/profiles/${repoId}/retry`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'manual_retry' }),
        },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'profiles'] });
      qc.invalidateQueries({ queryKey: ['ecosystem', 'repo-full'] });
    },
  });
}

// ============================================================
// v1.5.0-E: Lifecycle batch dispatch (deep_review_request_batch)
// ============================================================

export interface LifecycleBatchIntent {
  repo_id: string;
  repo_full_name: string;
  deep_review_id: string;
  prompt: string;
  timeout_seconds: number;
  project_id: string | null;
}

export interface LifecycleBatchResponse {
  success: boolean;
  dispatched: number;
  intents: LifecycleBatchIntent[];
}

export interface LifecycleBatchInput {
  tags: string[];
  min_stars?: number | null;
  limit?: number;
  research_goal?: string;
}

export function useLifecycleRequestBatch() {
  return useMutation({
    mutationFn: async (input: LifecycleBatchInput) =>
      apiFetch<LifecycleBatchResponse>(`/api/ecosystem/lifecycle/request_batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tags: input.tags,
          min_stars: input.min_stars ?? null,
          limit: input.limit ?? 20,
          research_goal: input.research_goal ?? '',
        }),
      }),
  });
}

// v1.6.0 event sourcing

export interface RepoEvent {
  id: string;
  event_type: string;
  payload_json: Record<string, unknown>;
  source: string;
  scan_run_id: string | null;
  from_status: string | null;
  to_status: string | null;
  reason: string | null;
  triggered_at: string;
}

export interface RepoEventsResponse {
  success: boolean;
  repo_id: string;
  events: RepoEvent[];
  total: number;
}

/** GET /api/ecosystem/repos/{repoId}/events */
export function useRepoEvents(repoId: string | null, limit = 50) {
  return useQuery({
    queryKey: ['ecosystem', 'repo-events', repoId, limit],
    queryFn: () =>
      apiFetch<RepoEventsResponse>(`/api/ecosystem/repos/${repoId}/events?limit=${limit}`),
    enabled: !!repoId,
    staleTime: 30_000,
  });
}

// ============================================================
// v1.6.0: 扫描研究历程 — events + deep_reviews 合并 timeline
// ============================================================

/** scan_history entry — 合并 event 与 deep_review，按时间倒序 */
export interface ScanHistoryEntry {
  kind: 'event' | 'deep_review';
  /** 事件类型 / 'deep_review_<stage_status>' */
  type: string;
  timestamp: string;
  /** 人类可读一句话 */
  summary: string;
  source?: string;
  scan_run_id?: string | null;
  /** event 详情 payload */
  payload?: Record<string, unknown>;
  /** deep_review 5 段式 markdown */
  expandable_md?: {
    summary?: string;
    architecture?: string;
    risks?: string;
    learnings?: string;
    integration?: string;
  };
  integration_recommendation?: string | null;
  stage_status?: string;
  review_id?: string;
}

export interface ScanHistoryResponse {
  success: boolean;
  repo_id: string;
  total: number;
  entries: ScanHistoryEntry[];
}

/** GET /api/ecosystem/repos/{repoId}/scan_history */
export function useScanHistory(repoId: string | null, limit = 50) {
  return useQuery({
    queryKey: ['ecosystem', 'scan-history', repoId, limit],
    queryFn: () =>
      apiFetch<ScanHistoryResponse>(`/api/ecosystem/repos/${repoId}/scan_history?limit=${limit}`),
    enabled: !!repoId,
    staleTime: 30_000,
  });
}

// ============================================================
// v1.7.0: 浅扫批次管理
// ============================================================

/** 浅扫批次 — 与后端 EcosystemShallowBatch 对齐 */
export interface ShallowBatch {
  id: string;
  project_id: string | null;
  triggered_by: string;
  trigger_reason: string | null;
  candidates_count: number;
  status: string; // pending_approval / approved / running / completed / cancelled
  approved_by: string | null;
  approved_at: string | null;
  completed_at: string | null;
  new_repos_count: number;
  updated_repos_count: number;
  metadata_changed_count: number;
  failed_count: number;
  created_at: string;
  updated_at: string;
}

/** 批次候选仓条目 */
export interface ShallowBatchItem {
  repo_id: string;
  repo_full_name: string;
  stars: number;
  stage_status: string;
  shallow_summary_excerpt: string;
  last_commit_at: string | null;
}

export interface ShallowBatchesResponse {
  batches: ShallowBatch[];
  total: number;
}

export interface ShallowBatchResponse {
  batch: ShallowBatch;
}

export interface ShallowBatchItemsResponse {
  batch_id: string;
  items: ShallowBatchItem[];
  total: number;
}

/** GET /api/ecosystem/shallow_batches */
export function useShallowBatches(limit = 50) {
  return useQuery({
    queryKey: ['ecosystem', 'shallow-batches', limit],
    queryFn: () =>
      apiFetch<ShallowBatchesResponse>(`/api/ecosystem/shallow_batches?limit=${limit}`),
    staleTime: 10_000,
  });
}

/** GET /api/ecosystem/shallow_batches/{batchId} */
export function useShallowBatch(batchId: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'shallow-batch', batchId],
    queryFn: () =>
      apiFetch<ShallowBatchResponse>(`/api/ecosystem/shallow_batches/${batchId}`),
    enabled: !!batchId,
    staleTime: 10_000,
  });
}

/** GET /api/ecosystem/shallow_batches/{batchId}/items */
export function useShallowBatchItems(batchId: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'shallow-batch-items', batchId],
    queryFn: () =>
      apiFetch<ShallowBatchItemsResponse>(`/api/ecosystem/shallow_batches/${batchId}/items`),
    enabled: !!batchId,
    staleTime: 15_000,
  });
}

/** POST /api/ecosystem/shallow_batches */
export function useCreateShallowBatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { triggered_by: string; trigger_reason?: string }) =>
      apiFetch<{ success: boolean; batch_id: string; candidates_count: number; status: string; message: string }>(
        `/api/ecosystem/shallow_batches`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(input),
        },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batches'] });
    },
  });
}

/** POST /api/ecosystem/shallow_batches/{batchId}/approve */
export function useApproveBatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ batchId, approvedBy }: { batchId: string; approvedBy: string }) =>
      apiFetch<{ success: boolean; batch_id: string; status: string; dr_created: number; message: string }>(
        `/api/ecosystem/shallow_batches/${batchId}/approve`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ approved_by: approvedBy }),
        },
      ),
    onSuccess: (_data, { batchId }) => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batches'] });
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batch', batchId] });
    },
  });
}

/** POST /api/ecosystem/shallow_batches/{batchId}/cancel */
export function useCancelBatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) =>
      apiFetch<{ success: boolean; batch_id: string; status: string; message: string }>(
        `/api/ecosystem/shallow_batches/${batchId}/cancel`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
      ),
    onSuccess: (_data, batchId) => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batches'] });
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batch', batchId] });
    },
  });
}
