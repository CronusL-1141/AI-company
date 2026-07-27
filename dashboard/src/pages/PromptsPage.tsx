import { useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { usePromptEffectiveness } from '@/api/promptRegistry';
import { FileCode2, TrendingUp } from 'lucide-react';
import { useT } from '@/i18n';

// The content-hash version columns (current hash / version count / total usage) were
// removed 2026-07-27 with the /versions endpoint — the /track endpoint that fed them
// had no callers, so those three columns rendered "-" for every row forever.

function SuccessRateBadge({ rate }: { rate: number | null }) {
  if (rate === null) return <span className="text-muted-foreground text-xs">-</span>;
  const color = rate >= 80 ? 'text-green-600' : rate >= 60 ? 'text-yellow-600' : 'text-red-500';
  return <span className={`text-sm font-semibold ${color}`}>{rate}%</span>;
}

function DurationBadge({ ms }: { ms: number | null }) {
  if (ms === null) return <span className="text-muted-foreground text-xs">-</span>;
  const s = (ms / 1000).toFixed(1);
  return <span className="text-xs text-muted-foreground">{s}s</span>;
}

export function PromptsPage() {
  const t = useT();
  const { data: effectivenessData, isLoading } = usePromptEffectiveness();

  const rows = useMemo(
    () =>
      (effectivenessData?.effectiveness ?? [])
        .slice()
        .sort((a, b) => b.total_activities - a.total_activities),
    [effectivenessData],
  );

  // Summary stats
  const totalTemplates = rows.length;
  const totalActivities = useMemo(
    () => rows.reduce((sum, e) => sum + e.total_activities, 0),
    [rows],
  );
  const avgSuccessRate = useMemo(() => {
    const withRate = rows.filter((e) => e.success_rate_pct !== null);
    if (withRate.length === 0) return null;
    const avg = withRate.reduce((sum, e) => sum + (e.success_rate_pct ?? 0), 0) / withRate.length;
    return Math.round(avg * 10) / 10;
  }, [rows]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <FileCode2 className="h-6 w-6" />
          Prompt Registry
        </h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t.prompts.subtitle}
        </p>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">{t.prompts.trackedTemplates}</CardTitle>
            <FileCode2 className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {isLoading ? <Skeleton className="h-8 w-12" /> : (
              <p className="text-2xl font-bold">{totalTemplates}</p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">{t.prompts.totalUsage}</CardTitle>
            <TrendingUp className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {isLoading ? <Skeleton className="h-8 w-12" /> : (
              <p className="text-2xl font-bold">{totalActivities}</p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">{t.prompts.avgSuccessRate}</CardTitle>
            <TrendingUp className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {isLoading ? <Skeleton className="h-8 w-12" /> : (
              <p className="text-2xl font-bold">
                {avgSuccessRate !== null ? `${avgSuccessRate}%` : '-'}
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Main table */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t.prompts.templateList}</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="p-6 space-y-3">
              {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}
            </div>
          ) : rows.length === 0 ? (
            <div className="py-12 text-center text-muted-foreground">
              <FileCode2 className="h-8 w-8 mx-auto mb-3 opacity-40" />
              <p>{t.prompts.noTemplates}</p>
              <p className="text-xs mt-1">{t.prompts.noTemplatesHint}</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t.prompts.colTemplateName}</TableHead>
                  <TableHead className="text-right">{t.prompts.colTotalActivities}</TableHead>
                  <TableHead className="text-right">{t.prompts.colSuccessRate}</TableHead>
                  <TableHead className="text-right">{t.prompts.colAvgDuration}</TableHead>
                  <TableHead className="text-right">{t.prompts.colFailureLessons}</TableHead>
                  <TableHead>{t.prompts.colTopFailureReasons}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((eff) => (
                  <TableRow key={eff.template_name}>
                    <TableCell className="font-medium text-sm">{eff.template_name}</TableCell>
                    <TableCell className="text-right text-sm">{eff.total_activities}</TableCell>
                    <TableCell className="text-right">
                      <SuccessRateBadge rate={eff.success_rate_pct} />
                    </TableCell>
                    <TableCell className="text-right">
                      <DurationBadge ms={eff.avg_duration_ms} />
                    </TableCell>
                    <TableCell className="text-right">
                      {eff.failure_lesson_count ? (
                        <Badge variant="outline" className="text-xs">{eff.failure_lesson_count}</Badge>
                      ) : (
                        <span className="text-muted-foreground text-xs">-</span>
                      )}
                    </TableCell>
                    <TableCell className="max-w-[220px]">
                      {eff.top_failure_reasons?.length ? (
                        <Tooltip>
                          <TooltipTrigger className="text-xs text-muted-foreground truncate block cursor-help text-left">
                            {eff.top_failure_reasons[0]}
                          </TooltipTrigger>
                          <TooltipContent className="max-w-xs">
                            <ul className="space-y-1 text-xs list-disc pl-3">
                              {eff.top_failure_reasons.map((r, i) => (
                                <li key={i}>{r}</li>
                              ))}
                            </ul>
                          </TooltipContent>
                        </Tooltip>
                      ) : (
                        <span className="text-muted-foreground text-xs">-</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
