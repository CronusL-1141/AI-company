import { Link, useParams } from 'react-router-dom';
import { useState } from 'react';
import {
  ArrowLeft,
  Star,
  ExternalLink,
  GitBranch,
  Calendar,
  Tag,
  AlertCircle,
  Archive,
  Clock,
  Code2,
  Info,
  RefreshCcw,
  AlertTriangle,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import {
  useEcosystemRepoDetail,
  useEcosystemRepoFull,
  useRetryFailedRepo,
  useScanHistory,
  DEEP_REVIEW_STATUS_LABELS,
  INTEGRATION_RECOMMENDATION_LABELS,
  STAGE_STATUS_LABELS,
  stageBadgeClass,
} from '@/api/ecosystem';
import { CapabilityTags } from '@/components/ecosystem/CapabilityTags';
import { DeepReviewSection } from '@/components/ecosystem/DeepReviewSection';
import { RelationsSection } from '@/components/ecosystem/RelationsSection';
import { ScanHistoryTimeline } from '@/components/ecosystem/ScanHistoryTimeline';
import { useToast } from '@/components/shared/useToast';
import { formatDate as formatDateOnly, serverTimeMs } from '@/lib/datetime';

/**
 * 单仓详情页 — 展示完整档案、元数据、评审记录 + 研究历程 timeline (v1.5.0-E)。
 * 路径：/ecosystem/:repoId
 */
export function EcosystemDetailPage() {
  const { repoId } = useParams<{ repoId: string }>();
  const { data: full, isLoading: isFullLoading } = useEcosystemRepoFull(repoId ?? null);
  const {
    data: fallbackRepo,
    isLoading: isFallbackLoading,
    error: fallbackError,
  } = useEcosystemRepoDetail(repoId ?? null);

  const repo = full?.profile ?? fallbackRepo;
  const isLoading = isFullLoading && isFallbackLoading;

  const retry = useRetryFailedRepo();
  const [retryDone, setRetryDone] = useState(false);
  const { showToast, toastNode } = useToast();
  const { data: scanHistoryData } = useScanHistory(repoId ?? null, 50);

  if (isLoading) {
    return (
      <div className="p-6 space-y-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-32 w-full" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Skeleton className="h-48" />
          <Skeleton className="h-48" />
        </div>
      </div>
    );
  }

  if (!repo) {
    return (
      <div className="p-6 max-w-4xl mx-auto">
        <Link to="/ecosystem">
          <Button variant="ghost" size="sm" className="mb-4">
            <ArrowLeft className="mr-1 h-4 w-4" aria-hidden="true" />
            返回列表
          </Button>
        </Link>
        <Card>
          <CardContent className="p-6 flex items-start gap-3 text-destructive">
            <AlertCircle className="h-5 w-5 mt-0.5 shrink-0" aria-hidden="true" />
            <div>
              <p className="font-medium">未找到该仓档案</p>
              <p className="text-sm text-muted-foreground mt-1">
                {fallbackError?.message ?? '请检查仓 ID 是否正确，或返回列表重新选择。'}
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  const formatDate = (iso: string | null | undefined): string =>
    formatDateOnly(iso, { year: 'numeric', month: '2-digit', day: '2-digit' }, '未知');

  const summary =
    repo.shallow_summary ||
    repo.one_line_summary ||
    repo.description_excerpt ||
    repo.description ||
    '暂无描述';

  // 提取最新一条评审记录的状态 + 集成建议
  const latestReview =
    full?.deep_reviews && full.deep_reviews.length > 0
      ? [...full.deep_reviews].sort(
          (a, b) => serverTimeMs(b.created_at) - serverTimeMs(a.created_at),
        )[0]
      : null;

  const reviewStatusStyle: Record<string, string> = {
    completed: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30',
    running: 'bg-blue-500/10 text-blue-600 border-blue-500/30',
    pending: 'bg-muted text-muted-foreground border-border',
    failed: 'bg-rose-500/10 text-rose-600 border-rose-500/30',
    skipped: 'bg-amber-500/10 text-amber-600 border-amber-500/30',
  };

  const recommendationStyle: Record<string, string> = {
    adopt: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/40',
    experiment: 'bg-blue-500/15 text-blue-700 dark:text-blue-300 border-blue-500/40',
    hold: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40',
    avoid: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/40',
  };

  // v1.6.0 SST: stage 来自 backend 单一源 (repo.stage_status), 不再客户端 derive.
  // backend _build_stage_map / _serialize_full 已统一派生逻辑.
  const inferredStage =
    (latestReview?.stage_status as string) || (repo.stage_status as string) || 'queued';

  const isFailed =
    inferredStage.endsWith('_failed') || (repo.fetch_failure_count ?? 0) >= 3;

  const onRetry = () => {
    if (retry.isPending) return;
    retry.mutate(repo.id, {
      onSuccess: (res) => {
        setRetryDone(true);
        setTimeout(() => setRetryDone(false), 3000);
        // 透出后端诚实提示——重置计数不等于立刻重新抓取，需等下次 tick
        showToast(res.next_action);
      },
    });
  };

  return (
    <div className="h-full overflow-y-auto">
      {toastNode}
      <div className="max-w-5xl mx-auto p-6 space-y-4">
        <div className="flex items-center gap-2">
          <Link to="/ecosystem">
            <Button variant="ghost" size="sm" aria-label="返回列表">
              <ArrowLeft className="mr-1 h-4 w-4" aria-hidden="true" />
              返回列表
            </Button>
          </Link>
        </div>

        {/* 头部卡片 */}
        <Card className={isFailed ? 'border-rose-300/60 bg-rose-50/30 dark:border-rose-700/40 dark:bg-rose-950/20' : ''}>
          <CardHeader>
            <div className="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
              <div className="min-w-0 flex-1">
                <CardTitle className="text-2xl flex items-center gap-2 flex-wrap break-all">
                  {repo.repo_full_name}
                  {/* v1.5.0-E: stage 徽章（统一颜色） */}
                  <span
                    className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${stageBadgeClass(inferredStage)}`}
                    title={`stage: ${inferredStage}`}
                  >
                    {STAGE_STATUS_LABELS[inferredStage as keyof typeof STAGE_STATUS_LABELS] ?? inferredStage}
                  </span>
                  {repo.is_archived && (
                    <Badge variant="outline" className="text-amber-600 border-amber-600">
                      <Archive className="mr-1 h-3 w-3" aria-hidden="true" />
                      已归档
                    </Badge>
                  )}
                  {repo.is_deleted && (
                    <Badge variant="outline" className="text-rose-600 border-rose-600/40 bg-rose-500/10">
                      <AlertTriangle className="mr-1 h-3 w-3" aria-hidden="true" />
                      仓已删除
                    </Badge>
                  )}
                  {latestReview && (
                    <Badge
                      variant="outline"
                      className={`text-xs ${reviewStatusStyle[latestReview.status] ?? ''}`}
                      title="评审当前 stage 进度"
                    >
                      {/* v1.5.2: 用 stage_status 而非 status，避免"浅扫 done 显示深扫已完成"的歧义 */}
                      评审:
                      {STAGE_STATUS_LABELS[latestReview.stage_status as keyof typeof STAGE_STATUS_LABELS] ??
                        DEEP_REVIEW_STATUS_LABELS[latestReview.status] ??
                        latestReview.status}
                    </Badge>
                  )}
                  {latestReview?.integration_recommendation && (
                    <Badge
                      variant="outline"
                      className={`text-xs font-medium ${recommendationStyle[latestReview.integration_recommendation] ?? ''}`}
                    >
                      建议:
                      {INTEGRATION_RECOMMENDATION_LABELS[latestReview.integration_recommendation] ??
                        latestReview.integration_recommendation}
                    </Badge>
                  )}
                </CardTitle>
                <p className="text-sm text-muted-foreground mt-2 leading-relaxed">{summary}</p>

                {/* failed 红色重试条 */}
                {isFailed && (
                  <div className="mt-3 flex items-start gap-2 rounded border border-rose-300/50 bg-rose-100/40 dark:bg-rose-950/30 px-3 py-2">
                    <AlertTriangle className="h-4 w-4 text-rose-600 mt-0.5 shrink-0" aria-hidden="true" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-rose-700 dark:text-rose-300">
                        抓取失败 — 失败次数 {repo.fetch_failure_count ?? 0}
                      </p>
                      {repo.last_fetch_error && (
                        <p className="text-xs text-rose-700/80 dark:text-rose-300/80 mt-0.5">
                          {repo.last_fetch_error}
                        </p>
                      )}
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      className="shrink-0 border-rose-400 text-rose-700 hover:bg-rose-100 dark:border-rose-700/50 dark:text-rose-300"
                      onClick={onRetry}
                      disabled={retry.isPending || retryDone}
                    >
                      <RefreshCcw className={`mr-1 h-3.5 w-3.5 ${retry.isPending ? 'animate-spin' : ''}`} aria-hidden="true" />
                      {retryDone ? '已入队' : '立即重试'}
                    </Button>
                  </div>
                )}
              </div>

              <div className="flex flex-col items-end gap-1.5 shrink-0">
                <div className="flex items-center gap-1.5 text-base font-semibold">
                  <Star className="h-4 w-4 text-yellow-500" aria-hidden="true" />
                  {repo.stars.toLocaleString()}
                </div>
                <a
                  href={`https://github.com/${repo.repo_full_name}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs text-primary hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded"
                >
                  GitHub
                  <ExternalLink className="h-3 w-3" aria-hidden="true" />
                </a>
                {repo.homepage && (
                  <a
                    href={repo.homepage}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-primary hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded"
                  >
                    主页
                    <ExternalLink className="h-3 w-3" aria-hidden="true" />
                  </a>
                )}
              </div>
            </div>
          </CardHeader>
        </Card>

        {/* Tabs：概览 / 扫描研究历程（v1.6.0：合并原 "研究历程" + "事件历史"） */}
        <Tabs defaultValue="overview">
          <TabsList variant="line" className="gap-3">
            <TabsTrigger value="overview">概览</TabsTrigger>
            <TabsTrigger value="scan-history">
              扫描研究历程
              {scanHistoryData && scanHistoryData.total > 0 && (
                <span className="ml-1.5 text-xs text-muted-foreground">({scanHistoryData.total})</span>
              )}
            </TabsTrigger>
          </TabsList>

          <TabsContent value="overview" className="mt-4 space-y-4">
            {/* 元数据网格 */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base flex items-center gap-2">
                    <Info className="h-4 w-4" aria-hidden="true" />
                    基本信息
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 text-sm">
                  <MetaRow label="拥有者" value={repo.owner} />
                  <MetaRow label="主语言" value={repo.language ?? '未识别'} icon={<Code2 className="h-3.5 w-3.5" />} />
                  {/* v1.6.0 SST: 删除"类别"行 — relevance_category 启发式分类不可信，
                      改由 GitHub topics 表达实际分类（详情页底部 topics 标签）
                      v1.6.1: 删除"相关性评分" + "活跃集排名" — 硬编码指标无参考价值 */}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-base flex items-center gap-2">
                    <Clock className="h-4 w-4" aria-hidden="true" />
                    时间轴
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 text-sm">
                  <MetaRow
                    label="最后提交"
                    value={formatDate(repo.last_commit_at)}
                    icon={<GitBranch className="h-3.5 w-3.5" />}
                  />
                  {repo.pushed_at && (
                    <MetaRow
                      label="最后 Push"
                      value={formatDate(repo.pushed_at)}
                      icon={<GitBranch className="h-3.5 w-3.5" />}
                    />
                  )}
                  <MetaRow
                    label="最后扫描"
                    value={formatDate(
                      // 取该仓任意一次扫描动作的最新时间（浅扫 / 批次入档）
                      [repo.last_shallow_refreshed_at, repo.last_scanned_at]
                        .filter((v): v is string => Boolean(v))
                        .sort((a, b) => serverTimeMs(b) - serverTimeMs(a))[0] ?? null,
                    )}
                    icon={<Calendar className="h-3.5 w-3.5" />}
                  />
                  <MetaRow
                    label="首次入档"
                    value={formatDate(repo.first_seen_at)}
                    icon={<Calendar className="h-3.5 w-3.5" />}
                  />
                </CardContent>
              </Card>
            </div>

            {/* Topics */}
            {repo.topics && repo.topics.length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-base flex items-center gap-2">
                    <Tag className="h-4 w-4" aria-hidden="true" />
                    GitHub Topics ({repo.topics.length})
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="flex flex-wrap gap-1.5">
                    {repo.topics.map((topic) => (
                      <Badge key={topic} variant="secondary" className="text-xs">
                        {topic}
                      </Badge>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}

            {/* 完整描述 */}
            {repo.description && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">仓库描述</CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-sm leading-relaxed text-muted-foreground whitespace-pre-wrap">
                    {repo.description}
                  </p>
                </CardContent>
              </Card>
            )}

            {/* 深度档案区 */}
            <div className="pt-2">
              <h2 className="text-sm font-semibold text-muted-foreground tracking-wide uppercase mb-3">
                深度档案
              </h2>

              {isFullLoading && !full ? (
                <div className="space-y-3">
                  <Skeleton className="h-32 w-full" />
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <Skeleton className="h-40" />
                    <Skeleton className="h-40" />
                  </div>
                </div>
              ) : !full ? (
                <Card className="border-dashed">
                  <CardContent className="p-4">
                    <div className="flex items-start gap-2 text-sm text-muted-foreground">
                      <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" aria-hidden="true" />
                      <div>
                        <p className="font-medium text-foreground">深度档案暂不可用</p>
                        <p className="mt-1 text-xs">
                          v2 API 调用失败 — 显示基础信息。该仓可能未深扫或服务暂不可达。
                        </p>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              ) : (
                <div className="space-y-4">
                  <CapabilityTags tags={full.tags} />
                  <DeepReviewSection reviews={full.deep_reviews} shallowSummary={repo.shallow_summary} />
                  <RelationsSection
                    outgoing={full.relations_from}
                    incoming={full.relations_to}
                    currentRepoFullName={full.profile.repo_full_name}
                  />
                </div>
              )}
            </div>
          </TabsContent>

          <TabsContent value="scan-history" className="mt-4">
            <ScanHistoryTimeline entries={scanHistoryData?.entries ?? []} />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}

function MetaRow({
  label,
  value,
  icon,
}: {
  label: string;
  value: string;
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 py-1 border-b border-border/40 last:border-0">
      <span className="text-muted-foreground text-xs flex items-center gap-1.5">
        {icon}
        {label}
      </span>
      <span className="font-medium text-right">{value}</span>
    </div>
  );
}

