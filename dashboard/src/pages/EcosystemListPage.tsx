import { useState, useMemo, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, Boxes, ChevronLeft, ChevronRight, Search as SearchIcon, Layers } from 'lucide-react';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { useEcosystemProfiles } from '@/api/ecosystem';
import type { EcosystemFilters } from '@/api/ecosystem';
import { RepoCard } from '@/components/ecosystem/RepoCard';
import { FilterBar } from '@/components/ecosystem/FilterBar';
import { EcosystemStatsBar } from '@/components/ecosystem/EcosystemStatsBar';
import { RecentScanRunsBar } from '@/components/ecosystem/RecentScanRunsBar';
import { EcosystemProjectFilter } from '@/components/ecosystem/EcosystemProjectFilter';
import { useProject } from '@/context/ProjectContext';

/**
 * Ecosystem 列表页 — v1.6.0：取消失活筛选，所有库永久参与搜索。
 * 项目筛选 = 本页头部的「生态库项目」下拉 (EcosystemProjectFilter，按项目隔离生态库)。
 * ⚠️ 这是 ecosystem 专属筛选，切勿移到全局 Header（历史教训详见 EcosystemProjectFilter.tsx）。
 * "显示已删除" 由 FilterBar 内 checkbox 控制（默认隐藏）。
 *
 * 路径：/ecosystem
 * 数据源：GET /api/ecosystem/profiles?facet_counts=true&is_deleted=...
 */
const PAGE_SIZE = 100;

export function EcosystemListPage() {
  const { projectId, projectName } = useProject();
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<EcosystemFilters>({
    limit: PAGE_SIZE,
    facetCounts: true,
  });

  // 筛选条件变化时重置到第 1 页（防止 page=3 但只剩 50 条空白）
  // 外部输入(filters)驱动的分页重置，合法 effect-setState；规则误报，具名豁免
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPage(1);
  }, [filters]);

  // v1.6.0 设计哲学：所有库永久参与搜索，不再默认排除已删除/失活仓。
  // FilterBar 勾选"显示已删除"时不变化（已含），未勾选时显式 isDeleted=false 仅看活跃仓。
  // - filters.isDeleted=null  → showDeleted 勾选 → 不传参（全集 265）
  // - filters.isDeleted=undefined（默认）→ 不传参（全集 265，符合 v1.6.0 哲学）
  // - filters.isDeleted=false → 仅未删除
  const effectiveFilters = useMemo<EcosystemFilters>(() => {
    return { ...filters, offset: (page - 1) * PAGE_SIZE };
  }, [filters, page]);

  const { data, isLoading, error } = useEcosystemProfiles(effectiveFilters);
  const profiles = useMemo(() => data?.profiles ?? [], [data]);

  // 客户端二次过滤：keyword 匹配 owner/description；v1.6.0 topics 多选 OR 求交集
  const filtered = useMemo(() => {
    const q = filters.keyword?.toLowerCase() ?? '';
    const selectedTopics = filters.topics ?? [];
    if (!q && selectedTopics.length === 0) return profiles;
    return profiles.filter((p) => {
      if (q) {
        const matchKw =
          p.repo_full_name.toLowerCase().includes(q) ||
          (p.owner ?? '').toLowerCase().includes(q) ||
          p.name.toLowerCase().includes(q) ||
          (p.description ?? p.description_excerpt ?? '').toLowerCase().includes(q) ||
          (p.one_line_summary ?? '').toLowerCase().includes(q);
        if (!matchKw) return false;
      }
      if (selectedTopics.length > 0) {
        const repoTopics = p.topics ?? [];
        // OR 语义：profile 的 topics 与 selected 有交集即通过
        const hit = selectedTopics.some((t) => repoTopics.includes(t));
        if (!hit) return false;
      }
      return true;
    });
  }, [profiles, filters.keyword, filters.topics]);

  // v1.6.0: 全局 topic 排名 map (StatsBar + RepoCard 共享)
  // 按 facet_counts.topics 排序后位置 → idx，让卡片标签颜色用全局 idx (不是卡片内部 idx)
  const topicRankMap = useMemo<Record<string, number>>(() => {
    const fc = data?.facet_counts?.topics ?? {};
    return Object.entries(fc)
      .sort(([, a], [, b]) => b - a)
      .reduce<Record<string, number>>((acc, [topic], idx) => {
        acc[topic] = idx;
        return acc;
      }, {});
  }, [data?.facet_counts?.topics]);

  return (
    <div className="flex h-full flex-col">
      {/* 页头 */}
      <div className="border-b px-6 py-4 bg-background">
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <Boxes className="h-5 w-5 text-primary" aria-hidden="true" />
              <h1 className="text-xl font-semibold">生态仓档案</h1>
            </div>
            <p className="text-sm text-muted-foreground mt-1">
              Claude Agent / MCP / Memory / Skill 等开源仓的广索引视图。点击卡片进入详情。
              {projectName && (
                <>
                  {' '}当前已按项目 <span className="text-primary font-medium">{projectName}</span> 过滤。
                </>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <span className="text-xs text-muted-foreground">生态库项目</span>
            <EcosystemProjectFilter />
            <Button
              variant="outline"
              size="sm"
              className="shrink-0"
              nativeButton={false}
              render={<Link to="/ecosystem/batches" />}
            >
              <Layers className="mr-1 h-4 w-4" aria-hidden="true" />
              浅扫批次
            </Button>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="shrink-0"
            nativeButton={false}
            render={<Link to="/ecosystem/research" />}
          >
            <SearchIcon className="mr-1 h-4 w-4" aria-hidden="true" />
            查找候选
          </Button>
        </div>
      </div>

      {/* 最近批次扫描概览（v1.5.2: 从单仓详情页迁回生态档案级位置） */}
      <RecentScanRunsBar />

      {/* 统计条 */}
      {!error && !isLoading && (
        <EcosystemStatsBar
          allProfiles={profiles}
          facetCounts={data?.facet_counts}
          total={data?.total}
        />
      )}

      {/* 筛选栏 */}
      <FilterBar
        filters={filters}
        onChange={setFilters}
        totalCount={filtered.length}
        facetCounts={data?.facet_counts}
      />

      {/* 列表区 */}
      <div className="flex-1 overflow-y-auto p-4">
        {isLoading && (
          <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-44 rounded-lg" />
            ))}
          </div>
        )}

        {error && (
          <div
            className="flex items-start gap-2 p-4 rounded-lg border border-destructive/50 bg-destructive/10 text-destructive"
            role="alert"
          >
            <AlertCircle className="h-5 w-5 mt-0.5 shrink-0" aria-hidden="true" />
            <div className="text-sm">
              <p className="font-medium">加载生态档案失败</p>
              <p className="mt-1 text-xs opacity-80">
                {error.message}
                {error.message.includes('Not Found') && (
                  <span className="block mt-1">
                    提示：API server 可能尚未注册 /api/ecosystem 路由，请确认后端已重启。
                  </span>
                )}
              </p>
            </div>
          </div>
        )}

        {!isLoading && !error && filtered.length === 0 && (
          <div className="flex flex-col items-center justify-center h-64 text-center text-muted-foreground">
            <Boxes className="h-10 w-10 opacity-30 mb-2" aria-hidden="true" />
            <p className="text-sm">暂无匹配的仓库</p>
            <p className="text-xs mt-1">
              {filters.keyword || filters.category || filters.minStars
                ? '调整筛选条件或清除过滤试试'
                : projectId
                  ? '当前项目下尚无生态仓档案，可切换到「全部项目」或运行扫描任务后填充'
                  : '运行扫描任务后将填充档案'}
            </p>
          </div>
        )}

        {!isLoading && !error && filtered.length > 0 && (
          <>
            <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {filtered.map((repo) => (
                <RepoCard key={repo.id} repo={repo} topicRankMap={topicRankMap} />
              ))}
            </div>

            {/* 分页器：仅在总数超过单页时显示 */}
            {(data?.total ?? 0) > PAGE_SIZE && (
              <div className="flex items-center justify-between mt-4 pt-4 border-t">
                <div className="text-sm text-muted-foreground">
                  第 <span className="font-medium text-foreground">{(page - 1) * PAGE_SIZE + 1}</span>
                  {' - '}
                  <span className="font-medium text-foreground">
                    {Math.min(page * PAGE_SIZE, data?.total ?? 0)}
                  </span>
                  {' '}/ 共 <span className="font-medium text-foreground">{data?.total ?? 0}</span> 仓
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page === 1}
                  >
                    <ChevronLeft className="h-4 w-4 mr-1" aria-hidden="true" />
                    上一页
                  </Button>
                  <span className="text-sm text-muted-foreground px-2">
                    第 {page} / {Math.ceil((data?.total ?? 0) / PAGE_SIZE)} 页
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setPage((p) => p + 1)}
                    disabled={!data?.has_more}
                  >
                    下一页
                    <ChevronRight className="h-4 w-4 ml-1" aria-hidden="true" />
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
