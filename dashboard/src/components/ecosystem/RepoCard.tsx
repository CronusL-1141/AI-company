import { Link } from 'react-router-dom';
import { useState } from 'react';
import {
  Star,
  GitBranch,
  Calendar,
  AlertCircle,
  Archive,
  RefreshCcw,
  AlertTriangle,
  FlaskConical,
} from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useToast } from '@/components/shared/useToast';
import type { EcosystemRepoProfile } from '@/api/ecosystem';
import { useRetryFailedRepo, TOPIC_COLOR_PALETTE } from '@/api/ecosystem';

interface RepoCardProps {
  /** 仓档案数据 */
  repo: EcosystemRepoProfile;
  /** 显式 stage（缺省时根据 profile 字段推断） */
  stage?: string;
  /** 全局 topic 排名 map (来自 facet_counts.topics)，决定 topic badge 颜色位置 */
  topicRankMap?: Record<string, number>;
}

/**
 * 格式化星标数：1234 -> 1.2k，12345 -> 12.3k，1234567 -> 1.2M
 */
function formatStars(stars: number): string {
  if (stars >= 1_000_000) return `${(stars / 1_000_000).toFixed(1)}M`;
  if (stars >= 1_000) return `${(stars / 1_000).toFixed(1)}k`;
  return String(stars);
}

/**
 * 计算距今天数 — 用于 last_commit_at 显示。
 */
function daysSince(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  return Math.floor((Date.now() - then) / (1000 * 60 * 60 * 24));
}

/**
 * 单仓卡片 — 列表视图的基本单元。点击跳详情页。
 * v1.5.0-E: 加 stage 徽章 + failed 红色高亮 + 立即重试按钮。
 * v1.6.0 SST: stage 完全由后端 stage_status 派生，前端不再做兜底推断。
 */
export function RepoCard({ repo, stage: stageProp, topicRankMap }: RepoCardProps) {
  const lastCommitDays = daysSince(repo.last_commit_at);
  const isStale = lastCommitDays !== null && lastCommitDays > 180;
  const summary =
    repo.shallow_summary || repo.one_line_summary || repo.description_excerpt || repo.description || '暂无描述';

  // v1.6.0 SST: 用后端透出的 stage_status；缺省默认 queued
  const stage = stageProp ?? repo.stage_status ?? 'queued';
  const researchCount = repo.research_count ?? 0;
  const isFailed = stage.endsWith('_failed') || (repo.fetch_failure_count ?? 0) >= 3;
  const isDeleted = !!repo.is_deleted;
  const isPrivate = !!repo.is_private_now;

  const retry = useRetryFailedRepo();
  const [retryDone, setRetryDone] = useState(false);
  const { showToast, toastNode } = useToast();

  const onRetry = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (retry.isPending) return;
    retry.mutate(repo.id, {
      onSuccess: (res) => {
        setRetryDone(true);
        // 2 秒后清除成功提示
        setTimeout(() => setRetryDone(false), 2000);
        // 透出后端诚实提示——重置计数不等于立刻重新抓取，需等下次 tick
        showToast(res.next_action);
      },
    });
  };

  // failed 卡片用红色边框 + 浅色底
  const cardBorder = isFailed
    ? 'border-rose-300/60 bg-rose-50/40 dark:border-rose-700/40 dark:bg-rose-950/20 hover:border-rose-400'
    : 'hover:border-primary/50 hover:bg-accent/30';

  return (
    <>
      {toastNode}
      <Link
        to={`/ecosystem/${repo.id}`}
        className="block focus:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-lg"
        aria-label={`查看 ${repo.repo_full_name} 详情`}
      >
        <Card className={`h-full transition-colors ${cardBorder}`}>
          <CardContent className="p-4 space-y-2.5">
            {/* 头部：仓名 + star */}
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <h3 className="font-semibold text-sm leading-snug truncate">
                  {repo.repo_full_name}
                </h3>
                {repo.language && (
                  <p className="text-xs text-muted-foreground mt-0.5">{repo.language}</p>
                )}
              </div>
              <div className="flex items-center gap-1 text-xs text-muted-foreground shrink-0">
                <Star className="h-3.5 w-3.5 text-yellow-500" aria-hidden="true" />
                <span className="font-medium">{formatStars(repo.stars)}</span>
              </div>
            </div>

            {/* 徽章条：研究次数 + 异常状态（v1.5.1：去掉 stage 文字徽章，stage 细节在研究历程里看）*/}
            {(researchCount > 0 || isDeleted || isPrivate) && (
              <div className="flex flex-wrap items-center gap-1">
                {researchCount > 0 && (
                  <span
                    className="inline-flex items-center gap-0.5 rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary"
                    title={`已被研究 ${researchCount} 次（点击进详情查看研究历程：每次涉及的系统改动 / 相关性 / 是否采用）`}
                  >
                    <FlaskConical className="h-2.5 w-2.5" aria-hidden="true" />
                    研究 ×{researchCount}
                  </span>
                )}
                {isDeleted && (
                  <span className="inline-flex items-center gap-0.5 rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium text-rose-700 dark:text-rose-300">
                    <AlertTriangle className="h-2.5 w-2.5" aria-hidden="true" />
                    已删除
                  </span>
                )}
                {isPrivate && (
                  <span className="inline-flex items-center gap-0.5 rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium text-rose-700 dark:text-rose-300">
                    <AlertTriangle className="h-2.5 w-2.5" aria-hidden="true" />
                    被设私有
                  </span>
                )}
              </div>
            )}

            {/* 一句话摘要（优先 shallow_summary） */}
            <p className="text-xs text-muted-foreground line-clamp-2 min-h-[2rem]">
              {summary}
            </p>

            {/* 失败错误提示 + 重试按钮 */}
            {isFailed && (
              <div className="rounded border border-rose-300/40 bg-rose-100/50 dark:bg-rose-950/30 px-2 py-1.5">
                <div className="flex items-start gap-1.5">
                  <AlertTriangle className="h-3 w-3 text-rose-600 mt-0.5 shrink-0" aria-hidden="true" />
                  <div className="min-w-0 flex-1">
                    <p
                      className="text-[10px] text-rose-700 dark:text-rose-300 line-clamp-2"
                      title={repo.last_fetch_error || ''}
                    >
                      {repo.last_fetch_error || '抓取失败（次数 ≥ 3）'}
                    </p>
                    <p className="text-[10px] text-rose-600/70 mt-0.5">
                      失败次数 {repo.fetch_failure_count ?? 0}
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    size="xs"
                    className="shrink-0 border-rose-300 text-rose-700 hover:bg-rose-100 dark:border-rose-700/40 dark:text-rose-300"
                    onClick={onRetry}
                    disabled={retry.isPending || retryDone}
                    aria-label="立即重试"
                  >
                    <RefreshCcw className={`h-3 w-3 ${retry.isPending ? 'animate-spin' : ''}`} aria-hidden="true" />
                    {retryDone ? '已入队' : '重试'}
                  </Button>
                </div>
              </div>
            )}

            {/* 标签条：topics（v1.6.0：删除 relevance_category 启发式分类显示，颜色用 TOPIC_COLOR_PALETTE 按位置循环） */}
            {repo.topics && repo.topics.length > 0 && (
              <div className="flex flex-wrap items-center gap-1">
                {repo.topics.slice(0, 4).map((topic, idx) => {
                  // 优先用全局排名 (topicRankMap)，缺省 fallback 本地 idx
                  const globalIdx = topicRankMap?.[topic] ?? idx;
                  return (
                    <Badge
                      key={topic}
                      variant="outline"
                      className={`text-[10px] px-1.5 py-0 h-4 ${TOPIC_COLOR_PALETTE[globalIdx % TOPIC_COLOR_PALETTE.length]}`}
                    >
                      {topic}
                    </Badge>
                  );
                })}
                {repo.topics.length > 4 && (
                  <span className="text-[10px] text-muted-foreground">
                    +{repo.topics.length - 4}
                  </span>
                )}
              </div>
            )}

            {/* 底部状态条 */}
            <div className="flex items-center gap-2 text-[10px] text-muted-foreground pt-1 border-t">
              {lastCommitDays !== null && (
                <span className="flex items-center gap-1">
                  <GitBranch className="h-3 w-3" aria-hidden="true" />
                  {lastCommitDays === 0 ? '今天' : `${lastCommitDays} 天前`}
                </span>
              )}
              {repo.is_archived && (
                <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
                  <Archive className="h-3 w-3" aria-hidden="true" />
                  已归档
                </span>
              )}
              {isStale && !repo.is_archived && (
                <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
                  <Calendar className="h-3 w-3" aria-hidden="true" />
                  沉寂
                </span>
              )}
              {repo.needs_deep_review && !isFailed && (
                <span className="ml-auto flex items-center gap-1 text-blue-600 dark:text-blue-400">
                  <AlertCircle className="h-3 w-3" aria-hidden="true" />
                  待深扫
                </span>
              )}
              {/* v1.6.1: 删除"相关性 X/10" — relevance_score 硬编码评分无参考价值 */}
            </div>
          </CardContent>
        </Card>
      </Link>
    </>
  );
}
