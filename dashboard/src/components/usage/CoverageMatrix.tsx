import { AlertTriangle } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { MetricBadge } from '@/components/shared/MetricBadge';
import { cn } from '@/lib/utils';
import { useT } from '@/i18n';
import type { CoverageReport, CoverageRow, DispatchPath, EdgeCoverage } from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// ① 覆盖率矩阵 —— 第一屏第一块，不可折叠、不可关闭（设计 §5.2 ①）。
//
// 这是页面的**主体**，不是页眉：先回答"测到了多少"，再谈"测到的是谁烧的"。
// 三条硬规则写死在这个组件里：
//   1. 每行带口径列，ctx_last 行与 usage_sum 行必须一眼可分（不同底色 + 徽标）；
//   2. **没有合计行**——两个口径实测差 5~25 倍，任何跨行合计都是混口径；
//   3. "设计上不采集"是一个正式取值，不是空白——空白会被读成 bug。
// ─────────────────────────────────────────────────────────────────────────────

/** 覆盖率闸值配色：低于 50% 是红线区，50~90% 是警戒，90% 以上是健康。 */
function coverageTone(pct: number): string {
  if (pct >= 90) return 'text-emerald-600 dark:text-emerald-400';
  if (pct >= 50) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

/** 按口径给整行上底色。视觉分区是这张表最重要的一件事，不是装饰。 */
function rowTone(row: CoverageRow): string {
  if (!row.metric) return 'bg-muted/40';
  if (row.metric === 'ctx_last') return 'bg-amber-50/70 dark:bg-amber-950/20';
  return '';
}

function pctText(attributed: number, total: number): string {
  if (total <= 0) return '0.0%';
  return `${((attributed / total) * 100).toFixed(1)}%`;
}

/** 一行的三个数（分母为 null 时前两个不适用）。表格与窄屏卡片共用，避免两处各算一遍。 */
function rowNumbers(row: CoverageRow) {
  const total = row.dispatches_total ?? 0;
  const attributed = row.dispatches_attributed ?? 0;
  return {
    // null 分母 = 这条路径上"测到多少"这个问题不适用。渲染成正式取值而不是空白：
    // 空白会被读成 bug，0 会被读成"一次都没测到"。
    notCollected: row.dispatches_total === null,
    total,
    attributed,
    unattributed: total - attributed,
  };
}

export interface CoverageMatrixProps {
  report: CoverageReport;
  /** 点击某一行的未归因数字时，把抽屉切到那条路径 */
  onFocusPath?: (path: DispatchPath) => void;
}

export function CoverageMatrix({ report, onFocusPath }: CoverageMatrixProps) {
  const t = useT();

  const pathName: Record<DispatchPath, string> = {
    subagent: t.usage.pathSubagent,
    leader_session: t.usage.pathLeader,
    workflow_self_report: t.usage.pathWorkflowSelfReport,
    tool_call: t.usage.pathToolCall,
  };

  // 最窄的一跳 = 端到端归因的天花板。R5 要求它与覆盖率同屏披露，否则一个漂亮的
  // 总覆盖率会把真正的瓶颈藏起来（实测瓶颈从来不在 token 采集，而在 agent→task）。
  const narrowest = report.hops.reduce<EdgeCoverage | null>((worst, hop) => {
    if (hop.required <= 0) return worst;
    if (!worst) return hop;
    return hop.resolvable / hop.required < worst.resolvable / worst.required ? hop : worst;
  }, null);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t.usage.matrixTitle}</CardTitle>
        <CardDescription>{t.usage.matrixDesc}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* 窄屏：堆叠卡片。表格在 375px 下要横向滚动才看得到数字，而这一块是页面的
            主体 —— 把主体藏进一个没有提示的横滚容器里，等于第一屏什么都没说。 */}
        <div className="space-y-2 md:hidden">
          {report.rows.map((row) => {
            const { notCollected, total, attributed, unattributed } = rowNumbers(row);
            return (
              <div key={row.path} className={cn('rounded border p-3', rowTone(row))}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">{pathName[row.path] ?? row.path}</span>
                  {row.metric ? (
                    <MetricBadge metric={row.metric} />
                  ) : (
                    <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
                      {t.usage.notCollected}
                    </span>
                  )}
                </div>
                {!notCollected && (
                  <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
                    <span
                      className={cn(
                        'text-base font-medium tabular-nums',
                        coverageTone((attributed / (total || 1)) * 100),
                      )}
                    >
                      {pctText(attributed, total)}
                    </span>
                    <span className="tabular-nums text-muted-foreground">
                      {t.usage.colMeasured} {attributed.toLocaleString()} / {t.usage.colDispatches}{' '}
                      {total.toLocaleString()}
                    </span>
                    {unattributed > 0 && (
                      <button
                        type="button"
                        onClick={() => onFocusPath?.(row.path)}
                        className="rounded text-muted-foreground underline underline-offset-2 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                      >
                        {unattributed.toLocaleString()} {t.usage.unattributedShort}
                      </button>
                    )}
                  </div>
                )}
                {row.note && (
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{row.note}</p>
                )}
              </div>
            );
          })}
        </div>

        <div className="hidden md:block">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t.usage.colPath}</TableHead>
                <TableHead className="text-right">{t.usage.colDispatches}</TableHead>
                <TableHead className="text-right">{t.usage.colMeasured}</TableHead>
                <TableHead className="text-right">{t.usage.colCoverage}</TableHead>
                <TableHead>{t.usage.colMetric}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {report.rows.map((row) => {
                const { notCollected, total, attributed, unattributed } = rowNumbers(row);
                return (
                  <TableRow key={row.path} className={cn(rowTone(row), 'align-top')}>
                    <TableCell className="max-w-md">
                      <div className="font-medium">{pathName[row.path] ?? row.path}</div>
                      {row.note && (
                        <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                          {row.note}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {notCollected ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        total.toLocaleString()
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {notCollected ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        attributed.toLocaleString()
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {notCollected ? (
                        <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
                          {t.usage.notCollected}
                        </span>
                      ) : (
                        <div className="space-y-0.5">
                          <div
                            className={cn(
                              'font-medium tabular-nums',
                              coverageTone((attributed / (total || 1)) * 100),
                            )}
                          >
                            {pctText(attributed, total)}
                          </div>
                          {unattributed > 0 && (
                            <button
                              type="button"
                              onClick={() => onFocusPath?.(row.path)}
                              className="rounded text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                            >
                              {unattributed.toLocaleString()} {t.usage.unattributedShort}
                            </button>
                          )}
                        </div>
                      )}
                    </TableCell>
                    <TableCell>
                      {row.metric ? (
                        <MetricBadge metric={row.metric} />
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>

        {/* 合计行的位置刻意留白说明——不写这句，看的人只会以为忘了加 */}
        <p className="rounded border border-dashed px-3 py-2 text-xs text-muted-foreground">
          {t.usage.noTotalRow}
        </p>

        <div className="space-y-2">
          <div>
            <h3 className="text-sm font-medium">{t.usage.hopsTitle}</h3>
            <p className="text-xs text-muted-foreground">{t.usage.hopsDesc}</p>
          </div>
          {narrowest && (
            <div className="flex items-start gap-2 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>
                {t.usage.narrowestHop(
                  narrowest.edge,
                  pctText(narrowest.resolvable, narrowest.required),
                )}
                {narrowest.note ? ` — ${narrowest.note}` : ''}
              </span>
            </div>
          )}
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {report.hops.map((hop) => {
              const pct = hop.required > 0 ? (hop.resolvable / hop.required) * 100 : 0;
              return (
                <div key={hop.edge} className="rounded border px-3 py-2" title={hop.note}>
                  <div className="flex items-baseline justify-between gap-2">
                    <code className="text-xs">{hop.edge}</code>
                    <span className={cn('text-sm font-medium tabular-nums', coverageTone(pct))}>
                      {pctText(hop.resolvable, hop.required)}
                    </span>
                  </div>
                  <div className="tabular-nums text-xs text-muted-foreground">
                    {hop.resolvable.toLocaleString()} / {hop.required.toLocaleString()}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
