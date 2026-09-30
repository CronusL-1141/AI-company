import { useQueries, useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { APIResponse } from '@/types';

// ─────────────────────────────────────────────────────────────────────────────
// token 用量归因（docs/token-attribution-v1-design.md §5）——只读消费层。
//
// 这里的类型刻意逐字对齐后端 aiteam.types 的结构，包括**没有合计字段**这一条：
// 四层实测 95.6% 是 cache_read，任何"总量"都是"缓存读取量"的同义词，跨模型/跨
// 派工路径的比较会被系统性带偏。要合计由看的人自己加，并自己承担解释责任。
//
// 同理，用量数值永远与它的分母（dispatches_total）和口径（metric）同生共死：
// 一个 token 数脱离这两样就没有意义，本库同时存在两个正交口径、实测差 5~25 倍。
// ─────────────────────────────────────────────────────────────────────────────

/** 口径标签。三者互不相加：usage_sum 是消耗量，另两个是瞬时水位快照。 */
export type MetricLabel = 'usage_sum' | 'ctx_last' | 'ctx_watermark' | '';

/** 归因层级。task 是旁支不是第五级——它的可信度与前四档不是一个量级。 */
export type ScopeLevel = 'project' | 'session' | 'workflow_run' | 'agent' | 'task';

/** 派工路径。主会话与子 agent 量级差百倍，分列呈现、默认不合并。 */
export type DispatchPath = 'subagent' | 'leader_session' | 'workflow_self_report' | 'tool_call';

/** 这一次归因的数是怎么来的。alias_fallback = 有行走了别名兜底，整份答案降级标注。 */
export type AttributionMethod = 'transcript_parse' | 'self_report' | 'alias_fallback';

/** 未归因原因码。前三个是可下钻的派工级；by_design 是工具调用级，只作一行标签。 */
export type UnattributedCode =
  | 'no_transcript_path'
  | 'transcript_gone'
  | 'not_yet_measured'
  | 'by_design'
  | 'self_report_absent'
  | 'multi_task_unsplittable';

/**
 * 一次归因查询的完整答案。四层分列、无合计字段，分子分母与口径同层。
 * 命名刻意避开 "Token" 前缀：带 token 词干的标识符必须在
 * scripts/usage_surface.py 申报量纲，而类型名不是任何量纲的数值。
 */
export interface AttributionPayload {
  scope: ScopeLevel;
  scope_id: string;
  metric: MetricLabel;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  /** 分子：本 scope 内已测到用量的派工数 */
  dispatches_attributed: number;
  /** 分母：本 scope 内的派工总数（含没数据的行） */
  dispatches_total: number;
  /** 未归因派工按原因码分类计数，值与分母同单位 */
  unattributed_reasons: Record<string, number>;
  measured_window: [string, string] | null;
  method: AttributionMethod;
}

/** 覆盖率矩阵的一行。刻意零 token 数值字段——这张表回答的是"测到了多少比例"。 */
export interface CoverageRow {
  path: DispatchPath;
  metric: MetricLabel;
  /** null = 这条路径上"测到多少"这个问题不适用（设计上不采集），不是 0 也不是空白 */
  dispatches_total: number | null;
  dispatches_attributed: number | null;
  unattributed_reasons: Record<string, number>;
  note: string;
  /**
   * 这一行属于哪个 harness。同一条 path 会按 harness 分成多行，所以 path 单独已经
   * 不能唯一标识一行了——列表 key 与行标签都必须把这一维带上。
   * null = 该桶的行没有标注 harness（多为该维度引入之前的历史行），**不等于
   * claude-code**；两个 harness 的分子分母禁止相加。
   */
  harness: string | null;
}

/**
 * 未标注 harness 的桶在 key 与查询串里的写法。查询串里没有 null，只能靠一个显式字面量；
 * 与后端 routes/usage.py 的 UNLABELLED_HARNESS_PARAM 是同一个值。/unattributed 不传
 * harness 参数 = 不过滤。
 */
const UNLABELLED_HARNESS = 'unlabelled';

/**
 * 矩阵一行的身份。同一条 path 按 harness 分成多行，只拿 path 去 find 取到的永远是
 * 第一行（4d87eba2：点哪一行的「N 未归因」，抽屉都显示 codex 那一桶）——凡是"指向
 * 矩阵某一行"的状态、回调与请求，都必须带上这两维。
 */
export interface CoverageRowRef {
  path: DispatchPath;
  harness: string | null;
}

export function isSameCoverageRow(row: CoverageRow, ref: CoverageRowRef): boolean {
  return row.path === ref.path && (row.harness ?? null) === ref.harness;
}

/**
 * 一行的稳定标识，列表 key 用。未标注的桶取一个显式字面量而不是空串 —— 空串与
 * "名叫空串的 harness"必须分得开。
 */
export function coverageRowKey(row: CoverageRowRef): string {
  return `${row.path}:${row.harness ?? UNLABELLED_HARNESS}`;
}

/**
 * 行标签要不要写出 harness。标注了 harness 的行一律写；未标注的行只在"同名兄弟行
 * 也在场"时才写 —— 否则一张全未标注的表会每行挂一个"未标注 harness"，而 workflow
 * 自报那一行压根不按 harness 分桶，写上去是假话。
 */
export function harnessLabelNeeded(rows: CoverageRow[]): (row: CoverageRow) => boolean {
  const seen = new Set<string>();
  const duplicated = new Set<string>();
  for (const row of rows) {
    if (seen.has(row.path)) duplicated.add(row.path);
    seen.add(row.path);
  }
  return (row) => (row.harness ?? null) !== null || duplicated.has(row.path);
}

/** 归因链上一跳的可解析率。端到端覆盖率是各跳的乘积，标量会掩盖真正的瓶颈。 */
export interface EdgeCoverage {
  edge: string;
  resolvable: number;
  required: number;
  note: string;
}

export interface CoverageReport {
  rows: CoverageRow[];
  hops: EdgeCoverage[];
  window: [string, string] | null;
  generated_at: string;
}

/** 下钻候选。只有派工数，没有任何用量——用量必须逐个 scope 单独查才拿得到。 */
export interface ScopeCandidate {
  scope: ScopeLevel;
  scope_id: string;
  label: string;
  dispatches_total: number;
  dispatches_attributed: number;
}

export interface UnattributedSample {
  agent_id: string;
  name: string;
  project_id: string;
  transcript_path: string;
  created_at: string | null;
  /** 这一行的 harness；null = 未标注。抽屉据此核对样例与所选矩阵行同桶 */
  harness: string | null;
}

export interface UnattributedSamples {
  /** 实际扫过多少行——样例是样例不是分母，计数以覆盖率矩阵的全量扫描为准 */
  scanned: number;
  scan_limit: number;
  samples: Record<string, UnattributedSample[]>;
}

/** 单次实测卡的载荷。来自现场解析一份 transcript，不是台账值，也没有合计字段。 */
export interface SingleProbePayload {
  agent_id: string;
  agent_name: string;
  metric: MetricLabel;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  api_calls: number;
  model: string;
  model_source: string;
  transcript_path: string;
  prompt_excerpt: string;
  result_excerpt: string;
  duration_ms: number | null;
  started_at: string | null;
  ended_at: string | null;
}

// ─────────────────────────────────────────────────────────────────────────────
// hooks
// ─────────────────────────────────────────────────────────────────────────────

export function useUsageCoverage(days: number) {
  return useQuery({
    queryKey: ['usage', 'coverage', days],
    queryFn: async () => {
      const res = await apiFetch<APIResponse<CoverageReport>>(`/api/usage/coverage?days=${days}`);
      return res.data;
    },
  });
}

function attributionUrl(scope: ScopeLevel, scopeId: string, path: DispatchPath, days: number): string {
  const q = new URLSearchParams({
    metric: 'usage_sum',
    scope,
    scope_id: scopeId,
    population: path,
    days: String(days),
  });
  return `/api/usage/attribution?${q}`;
}

export function useAttribution(
  scope: ScopeLevel,
  scopeId: string,
  path: DispatchPath,
  days: number,
  enabled = true,
) {
  return useQuery({
    queryKey: ['usage', 'attribution', scope, scopeId, path, days],
    queryFn: async () => {
      const res = await apiFetch<APIResponse<AttributionPayload>>(
        attributionUrl(scope, scopeId, path, days),
      );
      return res.data;
    },
    enabled,
  });
}

/**
 * 一次性取回一组同级 scope 的归因结果，供"每级一张卡、按 output_tokens 排序"使用。
 * 逐个查而不是让后端一次返回一批：每张卡都必须带着自己的分母与未归因分类回来，
 * 一个批量端点很容易退化成一张只有数值的表。
 */
export function useAttributionBatch(
  scope: ScopeLevel,
  scopeIds: string[],
  path: DispatchPath,
  days: number,
) {
  return useQueries({
    queries: scopeIds.map((id) => ({
      queryKey: ['usage', 'attribution', scope, id, path, days],
      queryFn: async () => {
        const res = await apiFetch<APIResponse<AttributionPayload>>(
          attributionUrl(scope, id, path, days),
        );
        return res.data;
      },
    })),
  });
}

export function useScopeCandidates(
  scope: ScopeLevel,
  parentScope: ScopeLevel | '',
  parentId: string,
  path: DispatchPath,
  days: number,
  limit: number,
) {
  return useQuery({
    queryKey: ['usage', 'scopes', scope, parentScope, parentId, path, days, limit],
    queryFn: async () => {
      const q = new URLSearchParams({
        scope,
        parent_scope: parentScope,
        parent_id: parentId,
        population: path,
        days: String(days),
        limit: String(limit),
      });
      const res = await apiFetch<APIResponse<ScopeCandidate[]>>(`/api/usage/scopes?${q}`);
      return res.data;
    },
  });
}

export function useUnattributedSamples(row: CoverageRowRef, days: number, enabled = true) {
  return useQuery({
    queryKey: ['usage', 'unattributed', row.path, row.harness, days],
    queryFn: async () => {
      const q = new URLSearchParams({
        population: row.path,
        harness: row.harness ?? UNLABELLED_HARNESS,
        days: String(days),
        per_reason: '5',
      });
      const res = await apiFetch<APIResponse<UnattributedSamples>>(`/api/usage/unattributed?${q}`);
      return res.data;
    },
    enabled,
  });
}

/** 单次实测。只在用户显式指定一次派工后才发请求——它会现场读一份 transcript。 */
export function useSingleProbe(agentId: string) {
  return useQuery({
    queryKey: ['usage', 'probe', agentId],
    queryFn: async () => {
      const res = await apiFetch<APIResponse<SingleProbePayload>>(
        `/api/usage/probe?agent_id=${encodeURIComponent(agentId)}`,
      );
      return res.data;
    },
    enabled: Boolean(agentId),
    retry: false,
    staleTime: Infinity,
  });
}
