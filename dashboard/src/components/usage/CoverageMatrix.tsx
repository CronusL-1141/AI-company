import { AlertTriangle } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { MetricBadge } from '@/components/shared/MetricBadge';
import { cn } from '@/lib/utils';
import { useT } from '@/i18n';
import {
  coverageRowKey,
  harnessLabelNeeded,
  type CoverageReport,
  type CoverageRow,
  type CoverageNoteKey,
  type CoverageRowRef,
  type DispatchPath,
  type EdgeCoverage,
} from '@/api/usage';

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

/** 行 key 与回调都带 harness：同一条 path 按 harness 分成多行，只用 path 会撞 key、指错行。 */
function rowRef(row: CoverageRow): CoverageRowRef {
  return { path: row.path, harness: row.harness ?? null };
}

export interface CoverageMatrixProps {
  report: CoverageReport;
  /** 点击某一行的未归因数字时，把抽屉切到这一行（path 加 harness） */
  onFocusRow?: (row: CoverageRowRef) => void;
}

export function CoverageMatrix({ report, onFocusRow }: CoverageMatrixProps) {
  const t = useT();

  const pathName: Record<DispatchPath, string> = {
    subagent: t.usage.pathSubagent,
    leader_session: t.usage.pathLeader,
    workflow_self_report: t.usage.pathWorkflowSelfReport,
    tool_call: t.usage.pathToolCall,
  };

  const labelNeeded = harnessLabelNeeded(report.rows);
  const harnessLabel = (row: CoverageRow): string | null =>
    labelNeeded(row) ? t.usage.coverageByHarness(row.harness ?? null) : null;

  // 说明文字按界面语言渲染：后端只给键与参数（types.CoverageNote）。标注了 harness 的
  // 行再补一句"不与其他 harness 相加"。
  const noteText = (key: CoverageNoteKey, params: Record<string, number | string> = {}): string => {
    const note = t.usage.coverageNotes[key];
    return typeof note === 'function' ? note(Number(params.activities ?? 0)) : (note ?? '');
  };
  const rowNote = (row: CoverageRow): string =>
    [noteText(row.note_key, row.note_params), row.harness ? t.usage.harnessOnlyNote(row.harness) : '']
      .filter(Boolean)
      .join(' ');

  // 打了排除标记的行不进任何统计；排除了多少必须同屏写明，排除是修正，不是藏起来。
  const excluded = Object.entries(report.excluded_rows ?? {}).filter(([, n]) => n > 0);
  const excludedTotal = excluded.reduce((sum, [, n]) => sum + n, 0);
  const excludedDetail = excluded
    .map(([kind, n]) => `${t.usage.exclusionKinds[kind] ?? kind} ${n.toLocaleString()}`)
    .join(' · ');

  // 各跳按 harness 分组呈现：Codex 行混进 CC 的分母，两边的可解析率都失真。
  const hopGroups: { harness: string | null; hops: EdgeCoverage[] }[] = [];
  for (const hop of report.hops) {
    const harness = hop.harness ?? null;
    let group = hopGroups.find((g) => g.harness === harness);
    if (!group) {
      group = { harness, hops: [] };
      hopGroups.push(group);
    }
    group.hops.push(hop);
  }

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
            const harness = harnessLabel(row);
            return (
              <div key={coverageRowKey(rowRef(row))} className={cn('rounded border p-3', rowTone(row))}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">
                    {pathName[row.path] ?? row.path}
                    {harness && (
                      <span className="ml-1.5 text-xs font-normal text-muted-foreground">
                        {harness}
                      </span>
                    )}
                  </span>
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
                        onClick={() => onFocusRow?.(rowRef(row))}
                        className="rounded text-muted-foreground underline underline-offset-2 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                      >
                        {unattributed.toLocaleString()} {t.usage.unattributedShort}
                      </button>
                    )}
                  </div>
                )}
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{rowNote(row)}</p>
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
                const harness = harnessLabel(row);
                return (
                  <TableRow key={coverageRowKey(rowRef(row))} className={cn(rowTone(row), 'align-top')}>
                    {/* TableCell 默认 nowrap：说明文字必须在本列内换行，否则会溢出盖住右侧数字列。
                        不设 max-width —— 表格布局下它只会压窄列宽，文字照样溢出；本列吃剩余宽度，
                        min-w 保证中等宽度下说明不被挤成一字一行。 */}
                    <TableCell className="min-w-48 whitespace-normal">
                      {/* 列窄时 harness 标签整体折到下一行、左对齐，不在标签中间断开 */}
                      <div className="flex flex-wrap items-baseline gap-x-1.5 font-medium">
                        <span>{pathName[row.path] ?? row.path}</span>
                        {harness && (
                          <span className="text-xs font-normal whitespace-nowrap text-muted-foreground">
                            {harness}
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                        {rowNote(row)}
                      </div>
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
                              onClick={() => onFocusRow?.(rowRef(row))}
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
        {excludedTotal > 0 && (
          <p className="text-xs text-muted-foreground" data-testid="usage-excluded-rows">
            {t.usage.excludedRows(excludedTotal, excludedDetail)}
          </p>
        )}

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
                  hopGroups.length > 1
                    ? t.usage.hopWithHarness(
                        narrowest.edge,
                        t.usage.coverageByHarness(narrowest.harness ?? null),
                      )
                    : narrowest.edge,
                  pctText(narrowest.resolvable, narrowest.required),
                )}
                {` - ${noteText(narrowest.note_key)}`}
              </span>
            </div>
          )}
          {hopGroups.map((group) => (
            <div key={group.harness ?? 'unlabelled'} className="space-y-1.5">
              {hopGroups.length > 1 && (
                <h4 className="text-xs font-medium text-muted-foreground">
                  {t.usage.coverageByHarness(group.harness)}
                </h4>
              )}
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                {group.hops.map((hop) => {
                  const pct = hop.required > 0 ? (hop.resolvable / hop.required) * 100 : 0;
                  return (
                    <div
                      key={hop.edge}
                      className="rounded border px-3 py-2"
                      title={noteText(hop.note_key)}
                    >
                      <div className="flex items-baseline justify-between gap-2">
                        <code className="text-xs">{hop.edge}</code>
                        <span
                          className={cn(
                            'text-sm font-medium tabular-nums',
                            hop.required > 0 ? coverageTone(pct) : 'text-muted-foreground',
                          )}
                        >
                          {hop.required > 0 ? pctText(hop.resolvable, hop.required) : '—'}
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
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
