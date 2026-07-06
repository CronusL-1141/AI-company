import { useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  FileSearch,
  Building2,
  ShieldAlert,
  Lightbulb,
  Beaker,
  CheckCircle2,
  XCircle,
  Clock,
  Loader2,
  ExternalLink,
  ChevronDown,
  ChevronUp,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  DEEP_REVIEW_STATUS_LABELS,
  INTEGRATION_RECOMMENDATION_LABELS,
  STAGE_STATUS_LABELS,
  stageBadgeClass,
} from '@/api/ecosystem';
import type { EcosystemDeepReview } from '@/api/ecosystem';
import { useReportDetail } from '@/api/reports';

interface DeepReviewSectionProps {
  reviews: EcosystemDeepReview[];
  /** 浅扫摘要（profile 级，用于 stage=shallow_done 时显示，避免一堆"暂无数据"） */
  shallowSummary?: string | null;
}

/**
 * v1.5.2 fix: 后端某些 datetime 字段无 +00:00（如 completed_at），
 * 直接 new Date(...) 会按浏览器本地时区解析，与 started_at 的 UTC 形成时差。
 * 此函数显式按 UTC 解析裸字符串。
 */
function parseAsUtc(s: string | null | undefined): number {
  if (!s) return NaN;
  // 已含时区或 Z 后缀，原样解析
  if (/[zZ]|[+-]\d{2}:\d{2}$/.test(s)) {
    return new Date(s).getTime();
  }
  // 裸字符串：把 "2026-05-08 09:20:23.268270" 当作 UTC
  const normalized = s.replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1') + 'Z';
  return new Date(normalized).getTime();
}

/** 状态徽章 */
function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { className: string; Icon: typeof CheckCircle2 }> = {
    completed: {
      className: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30',
      Icon: CheckCircle2,
    },
    running: { className: 'bg-blue-500/10 text-blue-600 border-blue-500/30', Icon: Loader2 },
    pending: { className: 'bg-muted text-muted-foreground border-border', Icon: Clock },
    failed: { className: 'bg-rose-500/10 text-rose-600 border-rose-500/30', Icon: XCircle },
    skipped: { className: 'bg-amber-500/10 text-amber-600 border-amber-500/30', Icon: XCircle },
  };
  const { className, Icon } = map[status] ?? map.pending;
  const label = DEEP_REVIEW_STATUS_LABELS[status] ?? status;
  return (
    <Badge variant="outline" className={`text-xs ${className}`}>
      <Icon className={`h-3 w-3 mr-1 ${status === 'running' ? 'animate-spin' : ''}`} aria-hidden="true" />
      {label}
    </Badge>
  );
}

/** 集成建议徽章 */
function RecommendationBadge({ rec }: { rec: string | null }) {
  if (!rec) return null;
  const styles: Record<string, string> = {
    adopt: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/40',
    experiment: 'bg-blue-500/15 text-blue-700 dark:text-blue-300 border-blue-500/40',
    hold: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40',
    avoid: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/40',
  };
  const label = INTEGRATION_RECOMMENDATION_LABELS[rec] ?? rec;
  return (
    <Badge variant="outline" className={`text-xs font-medium ${styles[rec] ?? ''}`}>
      建议：{label}
    </Badge>
  );
}

/** Markdown 子节渲染 — 折叠/展开。空字段时显示"暂无数据"占位而非整段隐藏。 */
function MarkdownBlock({
  title,
  Icon,
  body,
  hideWhenEmpty = false,
}: {
  title: string;
  Icon: typeof Building2;
  body: string;
  /** true 时空字段彻底不渲染（用于 demo log 这类可选块），默认 false 显示占位 */
  hideWhenEmpty?: boolean;
}) {
  const isEmpty = !body || !body.trim();
  if (isEmpty && hideWhenEmpty) return null;
  return (
    <section className="space-y-1.5">
      <h4 className="text-sm font-semibold flex items-center gap-1.5">
        <Icon className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
        {title}
      </h4>
      <div className="md-prose text-sm max-w-none pl-5">
        {isEmpty ? (
          <p className="text-xs text-muted-foreground italic">暂无数据 — 该字段将在对应 stage 完成后填充</p>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
        )}
      </div>
    </section>
  );
}

/** 单条评审记录卡片（含浅扫 / 架构深扫 / 辩论 / 集成等多 stage） */
function ReviewCard({
  review,
  shallowSummary,
}: {
  review: EcosystemDeepReview;
  shallowSummary?: string | null;
}) {
  const [expanded, setExpanded] = useState(true);
  const [showFullReport, setShowFullReport] = useState(false);

  const { data: reportDetail, isLoading: reportLoading } = useReportDetail(
    showFullReport && review.report_id ? review.report_id : null,
  );

  const formatDate = (iso: string): string => {
    const ms = parseAsUtc(iso);
    if (Number.isNaN(ms)) return iso;
    return new Date(ms).toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  // 评审实际耗时：基于 started_at→(shallow_completed_at | completed_at) 的差值。
  // 历史 row duration_seconds 多为 0，前端按 UTC 解析裸字符串自算。
  const formatElapsed = (
    start: string | null | undefined,
    end: string | null | undefined,
  ): string => {
    if (!start || !end) return '—';
    const ms = parseAsUtc(end) - parseAsUtc(start);
    if (Number.isNaN(ms) || ms <= 0) return '—';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return `${sec}s`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ${sec % 60}s`;
    const hr = Math.floor(min / 60);
    return `${hr}h ${min % 60}m`;
  };

  const stage = (review.stage_status as string) ?? '';
  const isShallowOnly = stage === 'shallow_done' || stage === 'queued' || stage === 'shallow_failed';
  const stageLabel = stage
    ? STAGE_STATUS_LABELS[stage as keyof typeof STAGE_STATUS_LABELS] ?? stage
    : null;
  const stageClass = stage ? stageBadgeClass(stage) : '';

  // v1.5.2: 浅扫阶段 summary/architecture/risks/learnings 必空，shallow_summary 在 profile 上。
  // 改用 stage 决定显示哪些字段，避免"已完成"+"一堆暂无数据"的违和感。
  const showDeepFields = !isShallowOnly;
  const hasDeepContent = useMemo(
    () =>
      Boolean(
        review.summary_md ||
          review.architecture_md ||
          review.risks_md ||
          review.learnings_md ||
          review.demo_log_excerpt,
      ),
    [review],
  );

  // 浅扫完成时的耗时端点优先 shallow_completed_at（若 backend 没写 completed_at）
  const elapsedEnd = isShallowOnly
    ? review.shallow_completed_at ?? review.completed_at
    : review.completed_at;

  return (
    <div className="border rounded-md p-3 space-y-3 bg-card">
      <div className="flex flex-wrap items-center gap-2 justify-between">
        <div className="flex flex-wrap items-center gap-2">
          {/* v1.5.2: 有 stage_status 时只显示 stage 徽章，避免 status='running'+stage='shallow_done'
              同时显示「深扫中（spinner）」+「浅扫完成」的语义冲突。失败/集成等终态下退回 status badge。 */}
          {stageLabel ? (
            <Badge variant="outline" className={`text-xs ${stageClass}`} title="当前 stage 进度">
              {stageLabel}
            </Badge>
          ) : (
            <StatusBadge status={review.status} />
          )}
          <RecommendationBadge rec={review.integration_recommendation} />
          <span className="text-xs text-muted-foreground" title="创建时间">
            {formatDate(review.created_at)}
          </span>
          <span className="text-xs text-muted-foreground" title="started_at → 当前 stage 完成时间">
            耗时 {formatElapsed(review.started_at, elapsedEnd)}
          </span>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={() => setExpanded((v) => !v)}
          aria-label={expanded ? '收起评审详情' : '展开评审详情'}
        >
          {expanded ? (
            <>
              <ChevronUp className="h-3.5 w-3.5 mr-1" aria-hidden="true" />
              收起
            </>
          ) : (
            <>
              <ChevronDown className="h-3.5 w-3.5 mr-1" aria-hidden="true" />
              展开
            </>
          )}
        </Button>
      </div>

      {expanded && (
        <div className="space-y-3">
          {/* 浅扫阶段：直接展示 profile.shallow_summary，不显示深扫专属空字段 */}
          {isShallowOnly && shallowSummary && (
            <MarkdownBlock title="浅扫摘要" Icon={FileSearch} body={shallowSummary} />
          )}
          {isShallowOnly && stage !== 'queued' && (
            <p className="text-xs text-muted-foreground italic">
              当前为浅扫阶段。架构 / 风险 / 学习要点等字段将在进入深扫后填充。
            </p>
          )}

          {/* D5 收敛：在飞占位闸门统一按权威轴 stage==='queued' 判断；status 不再作
              权威判断（'running' 仅存在于历史行，孤儿值 'pending' 后端枚举从不产生，
              原两个 status 分支已合并删除）。注意不能再叠加 showDeepFields——
              queued 属 shallow-only，叠加即恒假死代码（V2 评审 finding）。 */}
          {!hasDeepContent && !review.report_id && stage === 'queued' && (
            <div className="flex items-start gap-2 text-xs text-blue-600 dark:text-blue-400 bg-blue-500/10 border border-blue-500/30 rounded p-2">
              <Loader2 className="h-3.5 w-3.5 mt-0.5 shrink-0 animate-spin" aria-hidden="true" />
              <span>评审进行中 — 当前 stage 完成后，本卡片对应字段将自动填充。可稍后刷新本页查看。</span>
            </div>
          )}
          {showDeepFields && (
            <>
              <MarkdownBlock title="摘要" Icon={FileSearch} body={review.summary_md} />
              <MarkdownBlock title="架构" Icon={Building2} body={review.architecture_md} />
              <MarkdownBlock title="风险" Icon={ShieldAlert} body={review.risks_md} />
              <MarkdownBlock title="学习要点" Icon={Lightbulb} body={review.learnings_md} />
            </>
          )}

          {(review.demo_result || review.demo_log_excerpt) && (
            <section className="space-y-1.5">
              <h4 className="text-sm font-semibold flex items-center gap-1.5">
                <Beaker className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
                Demo 运行
                {review.demo_result && (
                  <Badge variant="outline" className="text-[10px] ml-1">
                    {review.demo_result}
                  </Badge>
                )}
              </h4>
              {review.demo_log_excerpt && (
                <pre className="text-[11px] bg-muted/50 border rounded p-2 max-h-60 overflow-auto whitespace-pre-wrap break-words">
                  {review.demo_log_excerpt}
                </pre>
              )}
            </section>
          )}

          {review.report_id && (
            <div className="pt-2 border-t border-border/50">
              {!showFullReport ? (
                <Button
                  variant="outline"
                  size="sm"
                  className="text-xs"
                  onClick={() => setShowFullReport(true)}
                >
                  <ExternalLink className="h-3.5 w-3.5 mr-1.5" aria-hidden="true" />
                  查看完整报告
                </Button>
              ) : (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium">完整报告内容</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 px-2 text-xs"
                      onClick={() => setShowFullReport(false)}
                    >
                      收起
                    </Button>
                  </div>
                  {reportLoading ? (
                    <p className="text-xs text-muted-foreground">加载报告中...</p>
                  ) : reportDetail ? (
                    <div className="md-prose text-sm max-w-none border rounded p-3 bg-muted/30 max-h-96 overflow-auto">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {reportDetail.content}
                      </ReactMarkdown>
                    </div>
                  ) : (
                    <p className="text-xs text-muted-foreground">报告加载失败</p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 评审记录区 — 展示该仓所有评审记录（浅/深/辩/集成多 stage 共享一行，最新优先）。
 */
export function DeepReviewSection({ reviews, shallowSummary }: DeepReviewSectionProps) {
  if (!reviews || reviews.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <FileSearch className="h-4 w-4" aria-hidden="true" />
            评审记录
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          尚未对该仓库生成评审记录。先经浅扫生成 200-400 字摘要，再按相关性进入深扫调度。
        </CardContent>
      </Card>
    );
  }

  // 按时间倒序
  const sorted = [...reviews].sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <FileSearch className="h-4 w-4" aria-hidden="true" />
          评审记录 ({reviews.length})
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {sorted.map((review) => (
          <ReviewCard key={review.id} review={review} shallowSummary={shallowSummary} />
        ))}
      </CardContent>
    </Card>
  );
}
