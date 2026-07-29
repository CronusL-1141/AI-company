import { cn } from '@/lib/utils';
import { useT } from '@/i18n';
import type { MetricLabel } from '@/api/usage';

/**
 * 口径徽标 —— 页面上每一个用量数值旁边都必须挂一枚（设计 §4.4 / §5.3）。
 *
 * 本库同时存在三个正交口径，实测差 5~25 倍。一个数值脱离口径就没有意义，而
 * "看着都是 token 数、于是放在一起比"正是本仓刚在时间戳上栽过的同类事故。
 * 视觉上三者必须一眼可分：不同底色 + 不同文字，不能只靠 tooltip。
 */
export interface MetricBadgeProps {
  metric: MetricLabel;
  className?: string;
  /** 紧凑模式：嵌在既有页面的数值旁边时用，只占一个小角标 */
  compact?: boolean;
}

const STYLES: Record<Exclude<MetricLabel, ''>, string> = {
  // usage_sum = 消耗量，是归因的正路，用中性主色
  usage_sum:
    'border-slate-300 bg-slate-100 text-slate-700 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200',
  // ctx_last = 瞬时水位快照，不是消耗量。琥珀色是刻意的"看清楚这不是同一回事"
  ctx_last:
    'border-amber-300 bg-amber-100 text-amber-800 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200',
  // ctx_watermark = 复用治理域，与用量归因无关
  ctx_watermark:
    'border-sky-300 bg-sky-100 text-sky-800 dark:border-sky-700 dark:bg-sky-950 dark:text-sky-200',
};

export function MetricBadge({ metric, className, compact = false }: MetricBadgeProps) {
  const t = useT();
  if (!metric) return null;

  const copy: Record<Exclude<MetricLabel, ''>, { name: string; what: string }> = {
    usage_sum: { name: t.usage.legendUsageSum, what: t.usage.legendUsageSumWhat },
    ctx_last: { name: t.usage.legendCtxLast, what: t.usage.legendCtxLastWhat },
    ctx_watermark: { name: t.usage.legendCtxWatermark, what: t.usage.legendCtxWatermarkWhat },
  };
  const { name, what } = copy[metric];

  return (
    <span
      className={cn(
        'inline-flex w-fit shrink-0 items-center rounded border font-mono font-medium whitespace-nowrap',
        compact ? 'px-1 py-0 text-[10px] leading-4' : 'px-1.5 py-0.5 text-[11px] leading-4',
        STYLES[metric],
        className,
      )}
      title={`${name} — ${what}`}
      aria-label={`${name}: ${what}`}
    >
      {metric}
    </span>
  );
}
