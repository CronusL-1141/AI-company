import { ChevronRight, Crosshair } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { MetricBadge } from '@/components/shared/MetricBadge';
import { cn } from '@/lib/utils';
import { formatDate } from '@/lib/datetime';
import { useT } from '@/i18n';
import type { AttributionPayload } from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// ③ 已归因明细的一张卡（设计 §5.2 ③）。
//
// 四层分列、**不显示合计**：实测 cache_read 占 95.6%，任何"总量"实际上是"缓存
// 读取量"的同义词，跨模型/跨派工路径的比较会被系统性带偏。要合计由看的人自己加。
//
// 数值永远与分母同屏：这不是页面上的自觉，是后端类型层强制带回来的
// （dispatches_total / unattributed_reasons 与四层同生共死）。
// ─────────────────────────────────────────────────────────────────────────────

export function formatTokenCount(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(2)}B`;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return String(v);
}

export function coverageTone(pct: number): string {
  if (pct >= 90) return 'text-emerald-600 dark:text-emerald-400';
  if (pct >= 50) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

/** 四层的显示顺序：output 打头，因为它是唯一与"干了多少活"强相关的一层。 */
function layers(data: AttributionPayload) {
  return [
    { key: 'output', value: data.output_tokens, primary: true },
    { key: 'input', value: data.input_tokens, primary: false },
    { key: 'cache_creation', value: data.cache_creation_tokens, primary: false },
    { key: 'cache_read', value: data.cache_read_tokens, primary: false },
  ] as const;
}

export interface AttributionCardProps {
  data: AttributionPayload;
  title: string;
  subtitle?: string;
  /** 传入即渲染"下钻"按钮 */
  onDrill?: () => void;
  drillLabel?: string;
  /** 传入即渲染"取本次实测"按钮（只有 agent 档有意义） */
  onProbe?: () => void;
  className?: string;
  compact?: boolean;
}

export function AttributionCard({
  data,
  title,
  subtitle,
  onDrill,
  drillLabel,
  onProbe,
  className,
  compact = false,
}: AttributionCardProps) {
  const t = useT();
  const total = data.dispatches_total;
  const attributed = data.dispatches_attributed;
  const pct = total > 0 ? (attributed / total) * 100 : 0;
  const reasons = Object.entries(data.unattributed_reasons ?? {}).filter(([, n]) => n > 0);

  const layerName: Record<string, string> = {
    output: t.usage.layerOutput,
    input: t.usage.layerInput,
    cache_creation: t.usage.layerCacheCreation,
    cache_read: t.usage.layerCacheRead,
  };
  const methodName: Record<string, string> = {
    transcript_parse: t.usage.methodTranscript,
    self_report: t.usage.methodSelfReport,
    alias_fallback: t.usage.methodAliasFallback,
  };

  return (
    <div className={cn('rounded-lg border bg-card p-3', className)}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="truncate font-medium" title={title}>
              {title}
            </span>
            <MetricBadge metric={data.metric} compact />
          </div>
          {subtitle && (
            <div className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground" title={subtitle}>
              {subtitle}
            </div>
          )}
        </div>
        <div className="flex shrink-0 gap-1">
          {onProbe && (
            <Button variant="outline" size="sm" onClick={onProbe} className="h-7 px-2 text-xs">
              <Crosshair className="mr-1 h-3 w-3" aria-hidden />
              {t.usage.probeThis}
            </Button>
          )}
          {onDrill && (
            <Button variant="outline" size="sm" onClick={onDrill} className="h-7 px-2 text-xs">
              {drillLabel}
              <ChevronRight className="ml-0.5 h-3 w-3" aria-hidden />
            </Button>
          )}
        </div>
      </div>

      {/* 四层分列。没有第五个格子，也没有一行"合计" —— 这是刻意的空缺。 */}
      <div className={cn('mt-2 grid gap-1.5', compact ? 'grid-cols-2' : 'grid-cols-2 sm:grid-cols-4')}>
        {layers(data).map((layer) => (
          <div
            key={layer.key}
            className={cn(
              'rounded border px-2 py-1.5',
              layer.primary ? 'border-primary/40 bg-primary/5' : 'bg-muted/30',
            )}
          >
            <div className="truncate font-mono text-[10px] text-muted-foreground">
              {layerName[layer.key]}
            </div>
            <div
              className={cn('tabular-nums', layer.primary ? 'text-base font-semibold' : 'text-sm')}
              title={layer.value.toLocaleString()}
            >
              {formatTokenCount(layer.value)}
            </div>
          </div>
        ))}
      </div>
      <p className="mt-1 text-[10px] text-muted-foreground">{t.usage.noTotalField}</p>

      {/* 分母与未归因分类：数值永远不许脱离它们单独出现 */}
      <div className="mt-2 border-t pt-2">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs">
          <span className={cn('font-medium tabular-nums', coverageTone(pct))}>{pct.toFixed(1)}%</span>
          <span className="text-muted-foreground">{t.usage.coverageOf(attributed, total)}</span>
          <span className="ml-auto rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {methodName[data.method] ?? data.method}
          </span>
        </div>
        {reasons.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {reasons.map(([code, n]) => (
              <span
                key={code}
                className="rounded border border-dashed px-1.5 py-0.5 text-[10px] text-muted-foreground"
              >
                {code} · {n.toLocaleString()}
              </span>
            ))}
          </div>
        )}
        {data.measured_window && (
          <div className="mt-1 text-[10px] text-muted-foreground">
            {/* 服务端时间串一律走 @/lib/datetime 解析——库里只许有一个时钟（I11） */}
            {t.usage.measuredWindow}: {formatDate(data.measured_window[0])} –{' '}
            {formatDate(data.measured_window[1])}
          </div>
        )}
      </div>
    </div>
  );
}
