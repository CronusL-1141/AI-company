import { useState, useMemo } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { useEvents } from '@/api/events';
import type { Event } from '@/types';
import { AlertTriangle, Shield, Zap, TrendingDown, CheckCircle2, XCircle } from 'lucide-react';
import { useT } from '@/i18n';

// Failure alchemy categories
type FailureCategory = 'antibody' | 'vaccine' | 'catalyst' | 'unknown';

const CATEGORY_STYLE: Record<FailureCategory, { icon: React.ElementType; color: string; bg: string }> = {
  antibody: {
    icon: Shield,
    color: 'text-blue-600',
    bg: 'bg-blue-100 dark:bg-blue-900/30',
  },
  vaccine: {
    icon: Zap,
    color: 'text-yellow-600',
    bg: 'bg-yellow-100 dark:bg-yellow-900/30',
  },
  catalyst: {
    icon: TrendingDown,
    color: 'text-purple-600',
    bg: 'bg-purple-100 dark:bg-purple-900/30',
  },
  unknown: {
    icon: AlertTriangle,
    color: 'text-gray-500',
    bg: 'bg-gray-100 dark:bg-gray-800/30',
  },
};

function useCategoryLabels(): Record<FailureCategory, string> {
  const t = useT();
  return {
    antibody: t.failures.catAntibody,
    vaccine: t.failures.catVaccine,
    catalyst: t.failures.catCatalyst,
    unknown: t.failures.catUnknown,
  };
}

interface FailureRecord {
  id: string;
  task_title?: string;
  root_cause?: string;
  fix_plan?: string;
  category: FailureCategory;
  agent_template?: string;
  timestamp: string;
  source: string;
  raw: Event;
}

function extractFailures(events: Event[]): FailureRecord[] {
  return events.map((evt): FailureRecord => {
    const d = evt.data || {};
    const category = (d.category as FailureCategory) || 'unknown';
    return {
      id: evt.id,
      task_title: (d.task_title as string) || (d.title as string) || (d.task as string) || '',
      root_cause: (d.root_cause as string) || (d.reason as string) || (d.error as string) || '',
      fix_plan: (d.fix_plan as string) || (d.solution as string) || '',
      category: ['antibody', 'vaccine', 'catalyst'].includes(category) ? category as FailureCategory : 'unknown',
      agent_template: (d.agent_template as string) || (d.agent as string) || '',
      timestamp: evt.timestamp,
      source: evt.source,
      raw: evt,
    };
  });
}

function StatCard({
  title,
  value,
  icon: Icon,
  color,
  loading,
}: {
  title: string;
  value: number | string;
  icon: React.ElementType;
  color: string;
  loading?: boolean;
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
        <Icon className={`h-4 w-4 ${color}`} />
      </CardHeader>
      <CardContent>
        {loading ? <Skeleton className="h-8 w-12" /> : (
          <p className={`text-2xl font-bold ${color}`}>{value}</p>
        )}
      </CardContent>
    </Card>
  );
}

function CategoryBadge({ category }: { category: FailureCategory }) {
  const labels = useCategoryLabels();
  const style = CATEGORY_STYLE[category];
  const Icon = style.icon;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${style.bg} ${style.color}`}>
      <Icon className="h-3 w-3" />
      {labels[category]}
    </span>
  );
}

export function FailuresPage() {
  const t = useT();
  const labels = useCategoryLabels();
  const [categoryFilter, setCategoryFilter] = useState<string>('all');

  // Query failure events
  const { data: eventsData, isLoading } = useEvents({
    type: 'failure_analysis',
    limit: 100,
  });

  // Also query task_failed events for additional data
  const { data: taskFailedData } = useEvents({
    type: 'task_failed',
    limit: 100,
  });

  const allEvents = useMemo(() => {
    const a = eventsData?.data ?? [];
    const b = taskFailedData?.data ?? [];
    return [...a, ...b].sort(
      (x, y) => new Date(y.timestamp).getTime() - new Date(x.timestamp).getTime(),
    );
  }, [eventsData, taskFailedData]);

  const failures = useMemo(() => extractFailures(allEvents), [allEvents]);

  const filtered = useMemo(() => {
    if (categoryFilter === 'all') return failures;
    return failures.filter((f) => f.category === categoryFilter);
  }, [failures, categoryFilter]);

  // Stats
  const totalCount = failures.length;
  const byCat = useMemo(() => {
    const counts: Record<FailureCategory, number> = { antibody: 0, vaccine: 0, catalyst: 0, unknown: 0 };
    failures.forEach((f) => { counts[f.category] = (counts[f.category] || 0) + 1; });
    return counts;
  }, [failures]);

  // Top root causes
  const topCauses = useMemo(() => {
    const causeMap: Record<string, number> = {};
    failures.forEach((f) => {
      if (f.root_cause && f.root_cause !== '') {
        const key = f.root_cause.slice(0, 60);
        causeMap[key] = (causeMap[key] || 0) + 1;
      }
    });
    return Object.entries(causeMap)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
  }, [failures]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <AlertTriangle className="h-6 w-6 text-red-500" />
          {t.failures.title}
        </h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t.failures.subtitle}
        </p>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard title={t.failures.totalFailures} value={totalCount} icon={XCircle} color="text-red-500" loading={isLoading} />
        <StatCard title={t.failures.antibodyRecords} value={byCat.antibody} icon={Shield} color="text-blue-600" loading={isLoading} />
        <StatCard title={t.failures.vaccineRecords} value={byCat.vaccine} icon={Zap} color="text-yellow-600" loading={isLoading} />
        <StatCard title={t.failures.catalystRecords} value={byCat.catalyst} icon={TrendingDown} color="text-purple-600" loading={isLoading} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Top failure causes */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{t.failures.topCauses}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {isLoading ? (
              Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-6 w-full" />)
            ) : topCauses.length === 0 ? (
              <p className="text-sm text-muted-foreground">{t.common.noData}</p>
            ) : (
              topCauses.map(([cause, count]) => (
                <div key={cause} className="flex items-center justify-between gap-2">
                  <span className="text-xs text-muted-foreground truncate flex-1" title={cause}>
                    {cause}
                  </span>
                  <Badge variant="secondary" className="shrink-0">{count}</Badge>
                </div>
              ))
            )}
          </CardContent>
        </Card>

        {/* Category distribution */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{t.failures.categoryDistribution}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {isLoading ? (
              Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-6 w-full" />)
            ) : (
              (Object.keys(CATEGORY_STYLE) as FailureCategory[]).map((cat) => {
                const count = byCat[cat];
                const pct = totalCount > 0 ? Math.round((count / totalCount) * 100) : 0;
                const cfg = CATEGORY_STYLE[cat];
                const Icon = cfg.icon;
                return (
                  <div key={cat} className="space-y-1">
                    <div className="flex items-center justify-between text-xs">
                      <span className={`flex items-center gap-1 ${cfg.color}`}>
                        <Icon className="h-3 w-3" />
                        {labels[cat]}
                      </span>
                      <span className="text-muted-foreground">{count} ({pct}%)</span>
                    </div>
                    <div className="w-full h-1.5 bg-muted rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all ${cfg.color.replace('text-', 'bg-')}`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                );
              })
            )}
          </CardContent>
        </Card>

        {/* Summary */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{t.failures.learningConversion}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {isLoading ? (
              <Skeleton className="h-20 w-full" />
            ) : (
              <>
                <div className="text-center py-2">
                  <p className="text-3xl font-bold text-green-600">
                    {totalCount > 0 ? `${Math.round(((byCat.antibody + byCat.vaccine) / totalCount) * 100)}%` : '-'}
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">{t.failures.convertedToAssets}</p>
                </div>
                <div className="text-xs text-muted-foreground space-y-1">
                  <p>{t.failures.antibodyDesc}</p>
                  <p>{t.failures.vaccineDesc}</p>
                  <p>{t.failures.catalystDesc}</p>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Failure list */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base">{t.failures.failureList}</CardTitle>
          <Select value={categoryFilter} onValueChange={(v) => setCategoryFilter(v ?? 'all')}>
            <SelectTrigger className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t.failures.allCategories}</SelectItem>
              {(Object.keys(CATEGORY_STYLE) as FailureCategory[]).map((cat) => (
                <SelectItem key={cat} value={cat}>{labels[cat]}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="p-6 space-y-3">
              {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}
            </div>
          ) : filtered.length === 0 ? (
            <div className="py-12 text-center text-muted-foreground">
              <CheckCircle2 className="h-8 w-8 mx-auto mb-3 opacity-40 text-green-500" />
              <p>{t.failures.noFailures}</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t.failures.colTask}</TableHead>
                  <TableHead>{t.failures.colCategory}</TableHead>
                  <TableHead>{t.failures.colRootCause}</TableHead>
                  <TableHead>{t.failures.colFixPlan}</TableHead>
                  <TableHead>{t.failures.colTemplate}</TableHead>
                  <TableHead>{t.failures.colTime}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((failure) => (
                  <TableRow key={failure.id}>
                    <TableCell className="max-w-[160px] truncate font-medium text-sm" title={failure.task_title}>
                      {failure.task_title}
                    </TableCell>
                    <TableCell>
                      <CategoryBadge category={failure.category} />
                    </TableCell>
                    <TableCell className="max-w-[200px] text-xs text-muted-foreground" title={failure.root_cause}>
                      <span className="line-clamp-2">{failure.root_cause}</span>
                    </TableCell>
                    <TableCell className="max-w-[200px] text-xs" title={failure.fix_plan}>
                      <span className="line-clamp-2">{failure.fix_plan || '-'}</span>
                    </TableCell>
                    <TableCell>
                      {failure.agent_template ? (
                        <Badge variant="outline" className="text-xs">{failure.agent_template}</Badge>
                      ) : (
                        <span className="text-muted-foreground text-xs">-</span>
                      )}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                      {new Date(failure.timestamp).toLocaleString('zh-CN', {
                        month: '2-digit',
                        day: '2-digit',
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
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
