import { useMemo } from 'react';
import { Boxes, FileSearch, Archive, Tag, FolderOpen, Folder } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import type { EcosystemRepoProfile, EcosystemFacetCounts } from '@/api/ecosystem';
import { TOPIC_COLOR_PALETTE } from '@/api/ecosystem';
import { useProject } from '@/context/ProjectContext';
import { serverTimeMs } from '@/lib/datetime';

interface EcosystemStatsBarProps {
  /** 完整列表（filter 之前），用于计算待深扫/失活/总数 */
  allProfiles: EcosystemRepoProfile[];
  /** 后端 facet_counts（启用 facetCounts=true 时返回） */
  facetCounts?: EcosystemFacetCounts;
  /** 后端汇报的总数（带后端筛选后的结果数） */
  total?: number;
}

/** 单个统计卡片 */
function StatCard({
  Icon,
  label,
  value,
  hint,
  tone = 'default',
}: {
  Icon: typeof Boxes;
  label: string;
  value: string | number;
  hint?: string;
  tone?: 'default' | 'primary' | 'warning' | 'info';
}) {
  const toneClass: Record<string, string> = {
    default: 'text-foreground',
    primary: 'text-primary',
    warning: 'text-amber-600 dark:text-amber-400',
    info: 'text-blue-600 dark:text-blue-400',
  };
  return (
    <Card className="px-3 py-2 flex flex-col gap-0.5 min-w-0 flex-1">
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground flex items-center gap-1">
        <Icon className="h-3 w-3" aria-hidden="true" />
        {label}
      </span>
      <span className={`text-xl font-semibold leading-none ${toneClass[tone]}`}>
        {value}
      </span>
      {hint && (
        <span className="text-[10px] text-muted-foreground truncate" title={hint}>
          {hint}
        </span>
      )}
    </Card>
  );
}

/**
 * 统计条 — ecosystem 列表页顶部的指标视图（按当前选中项目的生态库统计）。
 *
 * 语义说明（Release blocker 修复）：
 *   - 已深扫 = EcosystemDeepReview.status='completed' 真实行数（不是 needs_deep_review=False）
 *   - 待深扫 = profile.needs_deep_review=True 的仓数
 *   - 失活 = is_archived=True 或 last_commit_at > 365 天
 *   - 覆盖率 = 已深扫 / 总仓数
 *
 * 之前的 bug：用 `profile.needs_deep_review === false` 等同于"已深扫"导致显示 163，
 * 但真实只有 3 条 completed DeepReview。"needs_deep_review" 语义是"是否需要被深扫"，
 * false 包含了"低相关性自动跳过"和"已完成"两种情况，不能等同于已深扫。
 */
export function EcosystemStatsBar({
  allProfiles,
  facetCounts,
  total,
}: EcosystemStatsBarProps) {
  const { projectName } = useProject();

  const stats = useMemo(() => {
    const totalCount = total ?? allProfiles.length;
    // v1.5.1：待浅扫总数用后端 facet 全量值（不被 limit=200 截断）；
    // facet 不可用时退回到列表估算（标记为下界）。
    const stageFacet = facetCounts?.stage ?? {};
    const needsDeepCount =
      facetCounts?.stage !== undefined
        ? stageFacet.queued ?? 0
        : allProfiles.filter((p) => (p.stage_status ?? 'queued') === 'queued').length;
    // v1.6.0 "已归档"：优先用 last_active_status（GitHub archived / 人工标记无价值），
    // 缺字段则 fallback 到旧规则（is_archived || last_commit > 365 天）。
    const now = Date.now();
    const hasStatusField = allProfiles.some((p) => p.last_active_status != null);
    const archivedCount = hasStatusField
      ? allProfiles.filter(
          (p) =>
            p.last_active_status === 'archived' ||
            p.last_active_status === 'manual_archived',
        ).length
      : allProfiles.filter((p) => {
          if (p.is_archived) return true;
          if (!p.last_commit_at) return false;
          const days = (now - serverTimeMs(p.last_commit_at)) / (1000 * 60 * 60 * 24);
          return days > 365;
        }).length;
    // 已深扫总数 = stage 进入 architecture_done+ 的全量
    const deepDoneCount =
      (stageFacet.architecture_done ?? 0) +
      (stageFacet.debated ?? 0) +
      (stageFacet.referenced ?? 0) +
      (stageFacet.integrated ?? 0);
    const isFacetAvailable = facetCounts?.stage !== undefined;
    return { totalCount, needsDeepCount, archivedCount, deepDoneCount, isFacetAvailable };
  }, [allProfiles, facetCounts, total]);

  // v1.6.0 SST: Top 8 热门 topics（GitHub 原生 topics 维度，取代基于启发式的 category）
  // 用 facet_counts.topics 全量统计，不被 limit 截断
  const topTopics = useMemo(() => {
    if (!facetCounts?.topics) return [];
    return Object.entries(facetCounts.topics)
      .filter(([, n]) => n > 0)
      .sort(([, a], [, b]) => b - a)
      .slice(0, 8);
  }, [facetCounts]);

  return (
    <div className="flex flex-col gap-2 px-4 pt-3">
      {/* 当前项目 chip — 让用户始终知道在看哪个项目的数据 */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-muted-foreground">当前视图:</span>
        <Badge
          variant="outline"
          className={`text-xs gap-1 ${projectName ? 'border-primary/40 bg-primary/5 text-primary' : ''}`}
        >
          {projectName ? (
            <FolderOpen className="h-3 w-3" aria-hidden="true" />
          ) : (
            <Folder className="h-3 w-3" aria-hidden="true" />
          )}
          {projectName ?? '全部项目'}
        </Badge>
        {topTopics.length > 0 && (
          <>
            <span className="text-xs text-muted-foreground ml-2">热门 Topics:</span>
            <div className="flex items-center gap-1 flex-wrap">
              {topTopics.map(([topic, n], idx) => (
                <Badge
                  key={topic}
                  variant="secondary"
                  className={`text-[10px] gap-1 ${TOPIC_COLOR_PALETTE[idx % TOPIC_COLOR_PALETTE.length]}`}
                >
                  <Tag className="h-2.5 w-2.5" aria-hidden="true" />
                  {topic}
                  <span className="ml-0.5 opacity-70">{n}</span>
                </Badge>
              ))}
            </div>
          </>
        )}
      </div>

      {/* 数值卡片排（v1.5.1：用 facet stage 全量统计，不被 limit 截断）*/}
      <div className="flex flex-wrap gap-2">
        <StatCard
          Icon={Boxes}
          label="当前视图"
          value={stats.totalCount}
          hint="所有库永久参与搜索；勾选「显示已删除」可查看删除/转私有仓"
          tone="default"
        />
        <StatCard
          Icon={FileSearch}
          label="待浅扫"
          value={stats.needsDeepCount}
          hint="浅扫=读 README/CHANGELOG/release 摘要功能与设计方向（自动批量）"
          tone="info"
        />
        <StatCard
          Icon={FileSearch}
          label="已被研究"
          value={stats.deepDoneCount}
          hint="该仓被纳入过系统改动调研（卡片/详情可看研究次数与历程）"
          tone="primary"
        />
        <StatCard
          Icon={Archive}
          label="已归档"
          value={stats.archivedCount}
          hint="GitHub archived 或人工标记无价值"
          tone="warning"
        />
      </div>
    </div>
  );
}
