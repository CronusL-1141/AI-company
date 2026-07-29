import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { CoverageMatrix } from '@/components/usage/CoverageMatrix';
import { UnattributedDrawer } from '@/components/usage/UnattributedDrawer';
import { AttributionDrill } from '@/components/usage/AttributionDrill';
import { SingleProbeCard } from '@/components/usage/SingleProbeCard';
import { MetricLegend } from '@/components/usage/MetricLegend';
import { cn } from '@/lib/utils';
import { formatDateTimeSystemLocale } from '@/lib/datetime';
import { useT } from '@/i18n';
import { useUnattributedSamples, useUsageCoverage, type DispatchPath } from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// /usage —— token 用量归因（docs/token-attribution-v1-design.md §5）。
//
// 五块结构，**自上而下的顺序即优先级**，不许重排：
//   ① 覆盖率矩阵（第一屏第一块，不可折叠、不可关闭）—— 这是页面主体不是页眉
//   ② 未归因抽屉（紧贴矩阵，默认展开）
//   ③ 已归因明细（可下钻；主会话与子 agent 分列、默认不合并）
//   ④ 单次实测卡（豁免于覆盖率闸，代价是只能来自单次 transcript 解析）
//   ⑤ 口径说明（页脚常驻）
//
// 这是一个**内部诊断页**，不是首屏叙事材料 —— 它在侧栏靠后的位置，也刻意不塞进
// /analytics（那里是团队产出效率，与用量归因是两种读者心智，混在一起会让覆盖率
// 标注被稀释）。
// ─────────────────────────────────────────────────────────────────────────────

const WINDOWS = [0, 7, 30, 90];

export function UsagePage() {
  const t = useT();
  const queryClient = useQueryClient();
  const [days, setDays] = useState(0);
  const [drawerPath, setDrawerPath] = useState<DispatchPath>('subagent');
  const [probeAgentId, setProbeAgentId] = useState('');

  const coverage = useUsageCoverage(days);
  const samples = useUnattributedSamples(
    drawerPath === 'subagent' || drawerPath === 'leader_session' ? drawerPath : 'subagent',
    days,
  );

  // agent→task 是全链最窄的一跳，旁支卡的标题要如实印出它 —— 端到端的 task 级
  // 归因不可能高于这个数，藏起来就是让旁支卡看着比实际可信。
  const taskHop = coverage.data?.hops.find((h) => h.edge === 'agent->task');
  const taskPct =
    taskHop && taskHop.required > 0
      ? `${((taskHop.resolvable / taskHop.required) * 100).toFixed(1)}%`
      : '—';

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">{t.usage.title}</h1>
          <p className="text-sm text-muted-foreground">{t.usage.subtitle}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1" role="group" aria-label={t.usage.windowLabel}>
            {WINDOWS.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => setDays(d)}
                aria-pressed={d === days}
                className={cn(
                  'rounded border px-2 py-1 text-xs transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                  d === days ? 'border-primary bg-primary text-primary-foreground' : 'hover:bg-muted',
                )}
              >
                {d === 0 ? t.usage.windowAll : t.usage.windowDays(d)}
              </button>
            ))}
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => queryClient.invalidateQueries({ queryKey: ['usage'] })}
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {t.usage.refresh}
          </Button>
        </div>
      </header>

      {coverage.isLoading && <p className="text-sm text-muted-foreground">{t.common.loading}</p>}
      {coverage.isError && (
        <p className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {t.common.loadFailed((coverage.error as Error)?.message ?? '')}
        </p>
      )}

      {coverage.data && (
        <>
          {/* ① 第一屏第一块 —— 不可折叠、不可关闭 */}
          <CoverageMatrix report={coverage.data} onFocusPath={setDrawerPath} />

          {/* ② 紧贴矩阵，默认展开 */}
          <UnattributedDrawer
            report={coverage.data}
            samples={samples.data}
            path={drawerPath}
            onPathChange={setDrawerPath}
          />

          {/* ③ 已归因明细 */}
          <AttributionDrill days={days} taskCoveragePct={taskPct} onProbe={setProbeAgentId} />

          {/* ④ 单次实测 */}
          <SingleProbeCard agentId={probeAgentId} onAgentIdChange={setProbeAgentId} />

          <p className="text-right text-[11px] text-muted-foreground">
            {t.usage.generatedAt(formatDateTimeSystemLocale(coverage.data.generated_at))}
          </p>
        </>
      )}

      {/* ⑤ 页脚常驻 —— 取数失败时也要在，看数字的人不会去翻文档 */}
      <MetricLegend />
    </div>
  );
}
