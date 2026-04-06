import { useState, useMemo } from 'react';
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
import { usePromptVersions, usePromptEffectiveness } from '@/api/promptRegistry';
import type { PromptTemplate, PromptEffectiveness } from '@/api/promptRegistry';
import { FileCode2, Hash, TrendingUp, ChevronDown, ChevronRight } from 'lucide-react';
import { useT } from '@/i18n';

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

function VersionsPopover({ template }: { template: PromptTemplate }) {
  const [open, setOpen] = useState(false);
  const latest = template.versions[template.versions.length - 1];

  return (
    <div>
      <button
        className="flex items-center gap-1 text-xs font-mono text-muted-foreground hover:text-foreground transition-colors"
        onClick={() => setOpen((v) => !v)}
        type="button"
      >
        <Hash className="h-3 w-3" />
        {latest?.content_hash ?? '-'}
        {template.versions.length > 1 && (
          <>
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            <span className="text-primary">+{template.versions.length - 1}</span>
          </>
        )}
      </button>
      {open && template.versions.length > 1 && (
        <div className="mt-2 space-y-1 pl-2 border-l-2 border-muted">
          {template.versions.slice().reverse().map((v) => (
            <div key={v.content_hash} className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="font-mono">{v.content_hash}</span>
              <span>x{v.usage_count}</span>
              <span>{new Date(v.first_used_at).toLocaleDateString('zh-CN')}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function PromptsPage() {
  const t = useT();
  const { data: versionsData, isLoading: versionsLoading } = usePromptVersions();
  const { data: effectivenessData, isLoading: effectivenessLoading } = usePromptEffectiveness();

  // Merge versions + effectiveness by template_name
  const merged = useMemo(() => {
    const templates = versionsData?.templates ?? [];
    const effectiveness = effectivenessData?.effectiveness ?? [];
    const effMap = new Map<string, PromptEffectiveness>();
    effectiveness.forEach((e) => effMap.set(e.template_name, e));

    // Start from version list; supplement with effectiveness-only entries
    const seen = new Set<string>();
    const rows: Array<{ template: PromptTemplate | null; eff: PromptEffectiveness | null; name: string }> = [];

    templates.forEach((t) => {
      seen.add(t.template_name);
      rows.push({ template: t, eff: effMap.get(t.template_name) ?? null, name: t.template_name });
    });

    effectiveness.forEach((e) => {
      if (!seen.has(e.template_name)) {
        rows.push({ template: null, eff: e, name: e.template_name });
      }
    });

    return rows.sort((a, b) => {
      const au = a.template?.total_usage ?? a.eff?.total_activities ?? 0;
      const bu = b.template?.total_usage ?? b.eff?.total_activities ?? 0;
      return bu - au;
    });
  }, [versionsData, effectivenessData]);

  const isLoading = versionsLoading || effectivenessLoading;

  // Summary stats
  const totalTemplates = merged.length;
  const totalUsage = useMemo(
    () => (versionsData?.templates ?? []).reduce((sum, t) => sum + t.total_usage, 0),
    [versionsData],
  );
  const avgSuccessRate = useMemo(() => {
    const withRate = (effectivenessData?.effectiveness ?? []).filter((e) => e.success_rate_pct !== null);
    if (withRate.length === 0) return null;
    const avg = withRate.reduce((sum, e) => sum + (e.success_rate_pct ?? 0), 0) / withRate.length;
    return Math.round(avg * 10) / 10;
  }, [effectivenessData]);

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
              <p className="text-2xl font-bold">{totalUsage}</p>
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
          ) : merged.length === 0 ? (
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
                  <TableHead>{t.prompts.colCurrentHash}</TableHead>
                  <TableHead className="text-right">{t.prompts.colVersionCount}</TableHead>
                  <TableHead className="text-right">{t.prompts.colTotalUsage}</TableHead>
                  <TableHead className="text-right">{t.prompts.colSuccessRate}</TableHead>
                  <TableHead className="text-right">{t.prompts.colAvgDuration}</TableHead>
                  <TableHead className="text-right">{t.prompts.colFailureLessons}</TableHead>
                  <TableHead>{t.prompts.colTopFailureReasons}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {merged.map(({ template, eff, name }) => (
                  <TableRow key={name}>
                    <TableCell className="font-medium text-sm">{name}</TableCell>
                    <TableCell>
                      {template ? (
                        <VersionsPopover template={template} />
                      ) : (
                        <span className="text-muted-foreground text-xs">{t.prompts.notTracked}</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {template ? (
                        <Badge variant="secondary">{template.versions.length}</Badge>
                      ) : (
                        <span className="text-muted-foreground text-xs">-</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right text-sm">
                      {template?.total_usage ?? eff?.total_activities ?? '-'}
                    </TableCell>
                    <TableCell className="text-right">
                      <SuccessRateBadge rate={eff?.success_rate_pct ?? null} />
                    </TableCell>
                    <TableCell className="text-right">
                      <DurationBadge ms={eff?.avg_duration_ms ?? null} />
                    </TableCell>
                    <TableCell className="text-right">
                      {eff?.failure_lesson_count ? (
                        <Badge variant="outline" className="text-xs">{eff.failure_lesson_count}</Badge>
                      ) : (
                        <span className="text-muted-foreground text-xs">-</span>
                      )}
                    </TableCell>
                    <TableCell className="max-w-[220px]">
                      {eff?.top_failure_reasons?.length ? (
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
