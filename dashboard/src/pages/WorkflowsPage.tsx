import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Workflow,
  Coins,
  Wrench,
  Timer,
  ChevronRight,
  ArrowLeft,
  ArrowUpDown,
  FolderOpen,
  RefreshCw,
  Users,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useProjects } from '@/api/projects';
import {
  useWorkflows,
  useWorkflow,
  useWorkflowAgents,
  useReconcileWorkflows,
  type WorkflowRun,
  type WorkflowAgent,
  type WorkflowStatus,
} from '@/api/workflows';
import { useT } from '@/i18n';
import type { Translations } from '@/i18n/zh';

// ─────────────────────────────────────────────────────────────────────────────
// 格式化 helpers
// ─────────────────────────────────────────────────────────────────────────────

function fmtTokens(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return String(v);
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

const STATUS_STYLES: Record<string, string> = {
  planned: 'border-slate-400 text-slate-600 dark:text-slate-300',
  running: 'border-blue-400 bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300',
  completed: 'border-green-400 bg-green-50 text-green-700 dark:bg-green-950/40 dark:text-green-300',
  interrupted: 'border-amber-400 bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300',
  killed: 'border-red-400 bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300',
  failed: 'border-rose-400 bg-rose-50 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300',
};

function statusLabel(t: Translations, status: WorkflowStatus): string {
  switch (status) {
    case 'planned':
      return t.workflows.statusPlanned;
    case 'running':
      return t.workflows.statusRunning;
    case 'completed':
      return t.workflows.statusCompleted;
    case 'interrupted':
      return t.workflows.statusInterrupted;
    case 'killed':
      return t.workflows.statusKilled;
    case 'failed':
      return t.workflows.statusFailed;
    default:
      return status;
  }
}

function StatusBadge({ status }: { status: WorkflowStatus }) {
  const t = useT();
  return (
    <Badge
      variant="outline"
      className={cn('gap-1', STATUS_STYLES[status] ?? STATUS_STYLES.planned)}
    >
      {status === 'running' && (
        <span className="h-1.5 w-1.5 rounded-full bg-current animate-pulse" />
      )}
      {statusLabel(t, status)}
    </Badge>
  );
}

function PhaseStepper({ phases }: { phases: WorkflowRun['phases'] }) {
  const list = phases ?? [];
  if (list.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {list.map((p, i) => (
        <span key={`${p.index}-${i}`} className="flex items-center gap-1">
          <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-muted px-1 text-[10px] font-medium">
            {i + 1}
          </span>
          <span className="max-w-[140px] truncate text-[11px] text-muted-foreground">
            {p.title}
          </span>
          {i < list.length - 1 && (
            <ChevronRight className="h-3 w-3 text-muted-foreground/40" />
          )}
        </span>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// 列表页
// ─────────────────────────────────────────────────────────────────────────────

function WorkflowCard({ run }: { run: WorkflowRun }) {
  const t = useT();
  const planned = run.planned_agent_count + (run.dynamic_nodes || 0);
  return (
    <Link to={`/workflows/${encodeURIComponent(run.wf_id)}`} className="block">
      <Card className="transition-colors hover:border-primary/50 hover:bg-accent/30">
        <CardHeader className="pb-2">
          <div className="flex flex-wrap items-center gap-2">
            <Workflow className="h-4 w-4 shrink-0 text-muted-foreground" />
            <span className="font-medium">{run.name || run.wf_id}</span>
            <StatusBadge status={run.status} />
            {run.source && (
              <Badge variant="secondary" className="text-[10px]" title={t.workflows.source}>
                {run.source}
              </Badge>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <PhaseStepper phases={run.phases} />
          <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <Users className="h-3 w-3" />
              {t.workflows.agentsPlanVsActual}: <strong>{run.agent_count}</strong>/{planned}
            </span>
            <span className="flex items-center gap-1">
              <Coins className="h-3 w-3" />
              {fmtTokens(run.total_tokens)}
            </span>
            <span className="flex items-center gap-1">
              <Wrench className="h-3 w-3" />
              {run.total_tool_calls}
            </span>
            <span className="flex items-center gap-1">
              <Timer className="h-3 w-3" />
              {fmtDuration(run.duration_ms)}
            </span>
            {run.completed_at && (
              <span>
                {t.workflows.completedAt}: {new Date(run.completed_at).toLocaleString()}
              </span>
            )}
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

export function WorkflowsPage() {
  const t = useT();
  const [statusFilter, setStatusFilter] = useState<string>('__all__');
  const [projectFilter, setProjectFilter] = useState<string>('__all__');

  const { data: projectsData } = useProjects();
  const projects = projectsData?.data ?? [];

  const filters = useMemo(
    () => ({
      status: statusFilter === '__all__' ? undefined : statusFilter,
      project_id: projectFilter === '__all__' ? undefined : projectFilter,
    }),
    [statusFilter, projectFilter],
  );

  const { data, isLoading, error } = useWorkflows(filters);
  const runs = data ?? [];
  const reconcile = useReconcileWorkflows();

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <Workflow className="h-6 w-6" />
            {t.workflows.title}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">{t.workflows.subtitle}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={projectFilter} onValueChange={(v) => setProjectFilter(v ?? '__all__')}>
            <SelectTrigger className="h-8 w-[180px] text-sm">
              <FolderOpen className="mr-1.5 h-3.5 w-3.5" />
              <SelectValue placeholder={t.workflows.allProjects} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">{t.workflows.allProjects}</SelectItem>
              {projects.map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v ?? '__all__')}>
            <SelectTrigger className="h-8 w-[140px] text-sm">
              <SelectValue placeholder={t.workflows.allStatus} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">{t.workflows.allStatus}</SelectItem>
              <SelectItem value="planned">{t.workflows.statusPlanned}</SelectItem>
              <SelectItem value="running">{t.workflows.statusRunning}</SelectItem>
              <SelectItem value="completed">{t.workflows.statusCompleted}</SelectItem>
              <SelectItem value="interrupted">{t.workflows.statusInterrupted}</SelectItem>
              <SelectItem value="killed">{t.workflows.statusKilled}</SelectItem>
              <SelectItem value="failed">{t.workflows.statusFailed}</SelectItem>
            </SelectContent>
          </Select>
          <Button
            variant="outline"
            size="sm"
            onClick={() => reconcile.mutate(undefined)}
            disabled={reconcile.isPending}
          >
            <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', reconcile.isPending && 'animate-spin')} />
            {reconcile.isPending ? t.workflows.reconciling : t.workflows.reconcile}
          </Button>
        </div>
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i}>
              <CardContent className="pt-6">
                <Skeleton className="h-16 w-full" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : error ? (
        <p className="text-sm text-destructive">{t.workflows.loadFailed(error.message)}</p>
      ) : runs.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-muted-foreground">
            <Workflow className="mx-auto mb-3 h-8 w-8 opacity-40" />
            <p>{t.workflows.noWorkflows}</p>
            <p className="mt-1 text-xs">{t.workflows.noWorkflowsHint}</p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {runs.map((run) => (
            <WorkflowCard key={run.wf_id} run={run} />
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// 详情页
// ─────────────────────────────────────────────────────────────────────────────

type SortKey = 'label' | 'model' | 'tokens' | 'tool_calls' | 'duration_ms' | 'state';

function StatTile({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
}) {
  return (
    <Card>
      <CardContent className="pt-6">
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {icon}
          {label}
        </p>
        <p className="mt-1 text-2xl font-bold">{value}</p>
      </CardContent>
    </Card>
  );
}

function AgentStateBadge({ state }: { state: WorkflowAgent['state'] }) {
  const t = useT();
  const styles: Record<WorkflowAgent['state'], string> = {
    queued: 'border-slate-400 text-slate-600 dark:text-slate-300',
    running: 'border-blue-400 text-blue-700 dark:text-blue-300',
    done: 'border-green-400 text-green-700 dark:text-green-300',
  };
  const labels: Record<WorkflowAgent['state'], string> = {
    queued: t.workflows.agentState.queued,
    running: t.workflows.agentState.running,
    done: t.workflows.agentState.done,
  };
  return (
    <Badge variant="outline" className={cn('text-[10px]', styles[state] ?? styles.queued)}>
      {labels[state] ?? state}
    </Badge>
  );
}

function AgentTable({ agents }: { agents: WorkflowAgent[] }) {
  const t = useT();
  const [sortKey, setSortKey] = useState<SortKey>('tokens');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');

  const sorted = useMemo(() => {
    const arr = [...agents];
    arr.sort((a, b) => {
      let cmp = 0;
      switch (sortKey) {
        case 'tokens':
          cmp = (a.tokens ?? 0) - (b.tokens ?? 0);
          break;
        case 'tool_calls':
          cmp = (a.tool_calls ?? 0) - (b.tool_calls ?? 0);
          break;
        case 'duration_ms':
          cmp = (a.duration_ms ?? 0) - (b.duration_ms ?? 0);
          break;
        case 'label':
          cmp = (a.label ?? '').localeCompare(b.label ?? '');
          break;
        case 'model':
          cmp = (a.model ?? '').localeCompare(b.model ?? '');
          break;
        case 'state':
          cmp = (a.state ?? '').localeCompare(b.state ?? '');
          break;
      }
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return arr;
  }, [agents, sortKey, sortDir]);

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  }

  function SortHead({ label, k, className }: { label: string; k: SortKey; className?: string }) {
    return (
      <TableHead className={className}>
        <button
          type="button"
          onClick={() => toggleSort(k)}
          className={cn(
            'inline-flex items-center gap-1 hover:text-foreground',
            sortKey === k && 'text-foreground font-medium',
          )}
        >
          {label}
          <ArrowUpDown className="h-3 w-3 opacity-60" />
        </button>
      </TableHead>
    );
  }

  if (agents.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">{t.workflows.noAgents}</p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <SortHead label={t.workflows.colLabel} k="label" />
          <SortHead label={t.workflows.colModel} k="model" />
          <SortHead label={t.workflows.colTokens} k="tokens" className="text-right" />
          <SortHead label={t.workflows.colToolCalls} k="tool_calls" className="text-right" />
          <SortHead label={t.workflows.colDuration} k="duration_ms" className="text-right" />
          <SortHead label={t.workflows.colState} k="state" />
          <TableHead>{t.workflows.colLastTool}</TableHead>
          <TableHead>{t.workflows.colResult}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sorted.map((a) => (
          <TableRow key={a.id || a.cc_agent_id}>
            <TableCell className="font-medium">{a.label || a.cc_agent_id}</TableCell>
            <TableCell className="text-xs text-muted-foreground">{a.model ?? '—'}</TableCell>
            <TableCell className="text-right tabular-nums">{fmtTokens(a.tokens)}</TableCell>
            <TableCell className="text-right tabular-nums">{a.tool_calls ?? 0}</TableCell>
            <TableCell className="text-right tabular-nums">{fmtDuration(a.duration_ms)}</TableCell>
            <TableCell>
              <AgentStateBadge state={a.state} />
            </TableCell>
            <TableCell className="text-xs text-muted-foreground">{a.last_tool_name ?? '—'}</TableCell>
            <TableCell className="max-w-[280px] truncate text-xs text-muted-foreground" title={a.result_preview ?? ''}>
              {a.result_preview ?? '—'}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function WorkflowDetailPage() {
  const t = useT();
  const { wfId } = useParams<{ wfId: string }>();
  const { data: run, isLoading, error } = useWorkflow(wfId ?? '');
  const { data: agentsData } = useWorkflowAgents(wfId ?? '');
  const agents = agentsData ?? [];

  const backButton = (
    <Button variant="ghost" size="sm" className="-ml-2" render={<Link to="/workflows" />}>
      <ArrowLeft className="mr-1 h-4 w-4" />
      {t.workflows.backToList}
    </Button>
  );

  if (isLoading) {
    return (
      <div className="space-y-6">
        {backButton}
        <Skeleton className="h-8 w-64" />
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Card key={i}>
              <CardContent className="pt-6">
                <Skeleton className="h-8 w-20" />
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  if (error || !run) {
    return (
      <div className="space-y-6">
        {backButton}
        <div className="py-12 text-center">
          <p className="text-sm text-destructive">
            {error ? t.workflows.loadFailed(error.message) : t.workflows.notFound}
          </p>
        </div>
      </div>
    );
  }

  const planned = run.planned_agent_count + (run.dynamic_nodes || 0);
  const resultText =
    run.result == null
      ? ''
      : typeof run.result === 'string'
        ? run.result
        : JSON.stringify(run.result, null, 2);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        {backButton}
        <Workflow className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-xl font-semibold">{run.name || run.wf_id}</h1>
        <StatusBadge status={run.status} />
        {run.source && (
          <Badge variant="secondary" className="text-[10px]">
            {run.source}
          </Badge>
        )}
        {run.team_id && (
          <Button
            variant="outline"
            size="sm"
            className="ml-auto"
            render={<Link to={`/projects/${run.team_id}`} />}
          >
            <Users className="mr-1.5 h-3.5 w-3.5" />
            {t.workflows.viewTeam}
          </Button>
        )}
      </div>

      {/* 顶部总量条 */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatTile
          icon={<Coins className="h-3.5 w-3.5" />}
          label={t.workflows.totalTokens}
          value={fmtTokens(run.total_tokens)}
        />
        <StatTile
          icon={<Wrench className="h-3.5 w-3.5" />}
          label={t.workflows.totalToolCalls}
          value={run.total_tool_calls}
        />
        <StatTile
          icon={<Timer className="h-3.5 w-3.5" />}
          label={t.workflows.duration}
          value={fmtDuration(run.duration_ms)}
        />
        <StatTile
          icon={<Users className="h-3.5 w-3.5" />}
          label={t.workflows.agentsPlanVsActual}
          value={
            <span>
              {run.agent_count}
              <span className="text-base font-normal text-muted-foreground">/{planned}</span>
            </span>
          }
        />
      </div>

      {/* 运行信息 + 阶段 */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">{t.workflows.runInfo}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-xs text-muted-foreground">
          <PhaseStepper phases={run.phases} />
          <div className="flex flex-wrap gap-x-6 gap-y-1">
            {run.started_at && (
              <span>
                {t.workflows.startedAt}: {new Date(run.started_at).toLocaleString()}
              </span>
            )}
            {run.completed_at && (
              <span>
                {t.workflows.completedAt}: {new Date(run.completed_at).toLocaleString()}
              </span>
            )}
            {run.script_path && (
              <span className="max-w-full truncate" title={run.script_path}>
                {run.script_path}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* 结果摘要 */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">{t.workflows.summary}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="whitespace-pre-wrap text-sm text-muted-foreground">
            {run.summary || t.workflows.noSummary}
          </p>
          {resultText && (
            <div>
              <p className="mb-1 text-xs font-medium text-muted-foreground">{t.workflows.result}</p>
              <pre className="max-h-64 overflow-auto rounded bg-muted p-3 text-[11px] leading-relaxed">
                {resultText}
              </pre>
            </div>
          )}
        </CardContent>
      </Card>

      {/* 逐-agent 表格 */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">{t.workflows.agentList}</CardTitle>
        </CardHeader>
        <CardContent>
          <AgentTable agents={agents} />
        </CardContent>
      </Card>
    </div>
  );
}
