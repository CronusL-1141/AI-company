import { Progress } from '@/components/ui/progress';
import { useT } from '@/i18n';

interface ContextWatermarkBarProps {
  /** 0-100. null/undefined = no watermark data captured yet (agent not shown). */
  pct: number | null | undefined;
  tokens?: number | null;
  className?: string;
}

// Thresholds match the decision-layer color coding in
// docs/agent-reuse-design.md section 5.3: <60% healthy, 60-85% getting full,
// >=85% should slim-then-reuse or spawn fresh.
function colorClass(pct: number): string {
  if (pct >= 85) return 'bg-red-500';
  if (pct >= 60) return 'bg-yellow-500';
  return 'bg-green-500';
}

function textColorClass(pct: number): string {
  if (pct >= 85) return 'text-red-600 dark:text-red-400';
  if (pct >= 60) return 'text-yellow-700 dark:text-yellow-400';
  return 'text-muted-foreground';
}

/**
 * Small context-window usage bar for agent/session cards. Renders nothing
 * when no watermark has been captured yet — a missing bar means "not
 * measured", not "0%", so it must not be drawn as an empty bar.
 */
export function ContextWatermarkBar({ pct, tokens, className }: ContextWatermarkBarProps) {
  const t = useT();
  if (pct === null || pct === undefined) return null;
  const clamped = Math.min(100, Math.max(0, pct));
  const title =
    tokens !== null && tokens !== undefined
      ? t.contextWatermark.tooltip(Math.round(clamped * 10) / 10, tokens)
      : undefined;

  return (
    <div className={className} title={title}>
      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span>{t.contextWatermark.label}</span>
        <span className={textColorClass(clamped)}>{clamped.toFixed(1)}%</span>
      </div>
      <Progress value={clamped} className="h-1.5" indicatorClassName={colorClass(clamped)} />
    </div>
  );
}
