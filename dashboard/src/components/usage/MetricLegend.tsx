import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { MetricBadge } from '@/components/shared/MetricBadge';
import { useT } from '@/i18n';
import type { MetricLabel } from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// ⑤ 口径说明（页脚常驻）—— 设计 §5.2 ⑤。
//
// 写在页面里而不是文档里，理由很直白：看数字的人不会去翻文档。三个口径的中文
// 定义与 src/aiteam/types.py 的 TOKEN_METRIC_SPECS 同源（那份常量存在的理由就是
// "同一段解释别在页面、文档、注释里各写一版然后各自漂移"），改一处请两处同改。
// ─────────────────────────────────────────────────────────────────────────────

export function MetricLegend() {
  const t = useT();

  const entries: { metric: Exclude<MetricLabel, ''>; name: string; who: string; what: string }[] = [
    {
      metric: 'usage_sum',
      name: t.usage.legendUsageSum,
      who: t.usage.legendUsageSumWho,
      what: t.usage.legendUsageSumWhat,
    },
    {
      metric: 'ctx_last',
      name: t.usage.legendCtxLast,
      who: t.usage.legendCtxLastWho,
      what: t.usage.legendCtxLastWhat,
    },
    {
      metric: 'ctx_watermark',
      name: t.usage.legendCtxWatermark,
      who: t.usage.legendCtxWatermarkWho,
      what: t.usage.legendCtxWatermarkWhat,
    },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t.usage.legendTitle}</CardTitle>
        <CardDescription>{t.usage.legendLead}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl className="grid gap-3 md:grid-cols-3">
          {entries.map((entry) => (
            <div key={entry.metric} className="rounded border p-3">
              <dt className="flex flex-wrap items-center gap-2">
                <MetricBadge metric={entry.metric} />
                <span className="text-sm font-medium">{entry.name}</span>
              </dt>
              <dd className="mt-1 space-y-1">
                <p className="text-xs leading-relaxed">{entry.what}</p>
                <p className="font-mono text-[10px] break-all text-muted-foreground">{entry.who}</p>
              </dd>
            </div>
          ))}
        </dl>
        <div className="rounded border border-dashed p-3">
          <h3 className="text-sm font-medium">{t.usage.legendWhyNoSum}</h3>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{t.usage.legendWhyNoSumBody}</p>
        </div>
      </CardContent>
    </Card>
  );
}
