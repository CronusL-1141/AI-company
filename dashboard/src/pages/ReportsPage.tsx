import { useState, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Badge } from '@/components/ui/badge';
import { FileText, Search, AlertCircle, FolderOpen } from 'lucide-react';
import { useReports, useReportDetail } from '@/api/reports';
import type { ReportMeta } from '@/api/reports';
import { useProjects } from '@/api/projects';
import { useT } from '@/i18n';

function formatSize(bytes: number, t: ReturnType<typeof useT>): string {
  if (bytes >= 1024) {
    return `${(bytes / 1024).toFixed(1)} ${t.reports.kb}`;
  }
  return `${bytes} ${t.reports.bytes}`;
}

function typeBadgeColor(type: string): string {
  switch (type) {
    case 'research':
      return 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300';
    case 'design':
      return 'bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-300';
    case 'analysis':
      return 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300';
    case 'meeting-minutes':
      return 'bg-orange-100 text-orange-700 dark:bg-orange-900 dark:text-orange-300';
    default:
      return '';
  }
}

function ReportCard({
  report,
  selected,
  onClick,
  t,
}: {
  report: ReportMeta;
  selected: boolean;
  onClick: () => void;
  t: ReturnType<typeof useT>;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left rounded-lg border p-3 transition-colors hover:bg-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
        selected ? 'border-primary bg-accent' : 'border-border'
      }`}
      aria-pressed={selected}
      aria-label={report.topic}
    >
      <div className="flex items-start gap-2">
        <FileText className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium text-sm leading-snug">{report.topic}</p>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
              {report.author}
            </Badge>
            {report.report_type && (
              <span
                className={`inline-flex items-center rounded px-1.5 py-0 text-[10px] font-medium ${typeBadgeColor(report.report_type)}`}
              >
                {t.reports.types?.[report.report_type as keyof typeof t.reports.types] ?? report.report_type}
              </span>
            )}
            {report.date && (
              <span className="text-[11px] text-muted-foreground">{report.date}</span>
            )}
            <span className="text-[11px] text-muted-foreground ml-auto">
              {formatSize(report.size_bytes, t)}
            </span>
          </div>
        </div>
      </div>
    </button>
  );
}

function ReportContent({
  reportId,
  t,
}: {
  reportId: string | null;
  t: ReturnType<typeof useT>;
}) {
  const { data, isLoading, error } = useReportDetail(reportId);

  if (!reportId) {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        <div className="text-center space-y-2">
          <FileText className="mx-auto h-10 w-10 opacity-30" />
          <p className="text-sm">{t.reports.selectReport}</p>
        </div>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="space-y-3 p-4">
        <Skeleton className="h-6 w-1/2" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-4/5" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex h-full items-center justify-center text-destructive">
        <div className="flex items-center gap-2 text-sm">
          <AlertCircle className="h-4 w-4" />
          <span>{t.reports.loadFailed(error?.message ?? '')}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="border-b px-4 py-3 shrink-0">
        <h2 className="font-semibold text-base leading-snug">{data.topic}</h2>
        <div className="mt-1 flex gap-3 text-xs text-muted-foreground">
          <span>
            {t.reports.author}: {data.author}
          </span>
          {data.report_type && (
            <span>
              {t.reports.type}: {t.reports.types?.[data.report_type as keyof typeof t.reports.types] ?? data.report_type}
            </span>
          )}
          {data.date && (
            <span>
              {t.reports.date}: {data.date}
            </span>
          )}
          <span>
            {t.reports.size}: {formatSize(data.size_bytes, t)}
          </span>
        </div>
      </div>
      <div className="flex-1 overflow-auto p-6">
        <div className="md-prose max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {data.content}
          </ReactMarkdown>
        </div>
      </div>
    </div>
  );
}

export function ReportsPage() {
  const t = useT();
  const { data: projectsData } = useProjects();
  const projects = projectsData?.data ?? [];

  const [search, setSearch] = useState('');
  const [authorFilter, setAuthorFilter] = useState('__all__');
  const [projectFilter, setProjectFilter] = useState('__all__');
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // API params
  const apiProjectId = projectFilter === '__all__' ? undefined : projectFilter;
  const { data: reports = [], isLoading, error } = useReports(undefined, undefined, apiProjectId);

  const authors = useMemo(() => {
    const seen = new Set<string>();
    return reports.filter((r) => {
      if (seen.has(r.author)) return false;
      seen.add(r.author);
      return true;
    });
  }, [reports]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return reports.filter((r) => {
      const matchesAuthor = authorFilter === '__all__' || r.author === authorFilter;
      const matchesSearch =
        !q || r.topic.toLowerCase().includes(q) || r.author.toLowerCase().includes(q);
      return matchesAuthor && matchesSearch;
    });
  }, [reports, search, authorFilter]);

  return (
    <div className="flex h-full flex-col gap-0">
      {/* Header: title + project selector */}
      <div className="border-b px-6 py-4">
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-semibold">{t.reports.title}</h1>
          <Select value={projectFilter} onValueChange={(v) => setProjectFilter(v ?? '__all__')}>
            <SelectTrigger className="h-8 w-[200px] text-sm">
              <FolderOpen className="mr-1.5 h-3.5 w-3.5" />
              <SelectValue placeholder={t.reports.allProjects} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">{t.reports.allProjects}</SelectItem>
              {projects.map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Main two-panel layout */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left: report list */}
        <aside className="flex w-72 shrink-0 flex-col border-r">
          {/* Search + author filter in one row */}
          <div className="flex items-center gap-2 border-b p-3">
            <div className="relative flex-1">
              <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
              <Input
                placeholder={t.reports.searchPlaceholder}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8 h-8 text-sm"
                aria-label={t.reports.searchPlaceholder}
              />
            </div>
            <Select value={authorFilter} onValueChange={(v) => setAuthorFilter(v ?? '__all__')}>
              <SelectTrigger className="h-8 w-[120px] shrink-0 text-sm" aria-label={t.reports.filterAuthor}>
                <SelectValue placeholder={t.reports.filterAuthor} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">{t.reports.filterAuthor}</SelectItem>
                {authors.map((r) => (
                  <SelectItem key={r.author} value={r.author}>
                    {r.author}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* List */}
          <div className="flex-1 overflow-y-auto p-2 space-y-1">
            {isLoading && (
              <div className="space-y-1">
                <Skeleton className="h-16 w-full rounded-lg" />
                <Skeleton className="h-16 w-full rounded-lg" />
                <Skeleton className="h-16 w-full rounded-lg" />
              </div>
            )}
            {error && (
              <div className="flex items-center gap-2 p-3 text-destructive text-sm">
                <AlertCircle className="h-4 w-4 shrink-0" />
                <span>{t.reports.loadFailed(error.message)}</span>
              </div>
            )}
            {!isLoading && !error && filtered.length === 0 && (
              <div className="p-4 text-center text-sm text-muted-foreground space-y-1">
                <p>{t.reports.noReports}</p>
                <p className="text-xs">{t.reports.noReportsHint}</p>
              </div>
            )}
            {filtered.map((report) => (
              <ReportCard
                key={report.id}
                report={report}
                selected={selectedId === report.id}
                onClick={() => setSelectedId(report.id)}
                t={t}
              />
            ))}
          </div>
        </aside>

        {/* Right: content preview */}
        <main className="flex-1 overflow-hidden">
          <Card className="h-full rounded-none border-0 shadow-none">
            <CardContent className="h-full p-0">
              <ReportContent reportId={selectedId} t={t} />
            </CardContent>
          </Card>
        </main>
      </div>
    </div>
  );
}
