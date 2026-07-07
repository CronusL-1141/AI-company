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

export function fmtDuration(ms: number | null | undefined): string {
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

/** running/interrupted = live 展示口径（token 用 live_tokens 估值 + ≈ 前缀，泳道随轮询推进） */
function isLiveStatus(status: WorkflowStatus): boolean {
  return status === 'running' || status === 'interrupted';
}

function toMs(s: string | null | undefined): number | null {
  if (!s) return null;
  const v = new Date(s).getTime();
  return Number.isFinite(v) ? v : null;
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

export function StatusBadge({ status }: { status: WorkflowStatus }) {
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
              {isLiveStatus(run.status) ? (
                <span title={t.workflows.liveApprox}>≈{fmtTokens(run.live_tokens)}</span>
              ) : (
                fmtTokens(run.total_tokens)
              )}
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

// ─────────────────────────────────────────────────────────────────────────────
// 相位泳道（纯 div/CSS，零图表库依赖——design §10.8）
// 时间轴 t0 = min(started_at)；t1 终态取 completed_at（fallback max(started+duration)），
// live 取 max(last_activity_at) fallback Date.now()。bar 随 15s 轮询与 workflow.* 事件
// 失效自然右移，不做本地秒级 ticker。
// ─────────────────────────────────────────────────────────────────────────────

/** bar 颜色沿 STATUS_STYLES 色系：done→emerald / error·failed→red / running·progress→blue / 其余→muted */
function agentBarClass(state: string): string {
  if (state === 'done') return 'bg-emerald-500';
  if (state === 'error' || state === 'failed') return 'bg-red-500';
  if (state === 'running' || state === 'progress') return 'bg-blue-500 animate-pulse';
  return 'bg-muted-foreground/40';
}

interface SwimLaneRow {
  agent: WorkflowAgent;
  leftPct: number;
  widthPct: number;
  durationMs: number | null;
}

interface SwimLaneGroup {
  phaseIndex: number;
  title: string;
  rows: SwimLaneRow[];
}

export function PhaseSwimLane({ run, agents }: { run: WorkflowRun; agents: WorkflowAgent[] }) {
  const t = useT();
  const live = isLiveStatus(run.status);

  const model = useMemo(() => {
    if (agents.length === 0) return null;

    // ── 时间轴端点 ──
    const starts = [toMs(run.started_at), ...agents.map((a) => toMs(a.started_at))].filter(
      (v): v is number => v != null,
    );
    const t0 = starts.length > 0 ? Math.min(...starts) : (toMs(run.created_at) ?? Date.now());

    let t1: number;
    if (live) {
      const acts = [
        toMs(run.last_activity_at),
        ...agents.map((a) => toMs(a.last_activity_at)),
      ].filter((v): v is number => v != null);
      t1 = acts.length > 0 ? Math.max(...acts) : Date.now();
    } else {
      const completed = toMs(run.completed_at);
      if (completed != null) {
        t1 = completed;
      } else {
        const ends = agents
          .map((a) => {
            const s = toMs(a.started_at);
            if (s == null) return null;
            return a.duration_ms != null ? s + a.duration_ms : s;
          })
          .filter((v): v is number => v != null);
        t1 = ends.length > 0 ? Math.max(...ends) : t0;
      }
    }
    const span = Math.max(t1 - t0, 1); // 防除零（单点/时钟异常时退化为满宽 bar）

    // ── 逐 agent bar 几何 ──
    const rows: SwimLaneRow[] = agents.map((a) => {
      const start = toMs(a.started_at) ?? toMs(a.queued_at) ?? t0;
      let end: number;
      if (a.state === 'running' || a.state === 'progress') {
        // live bar 右端 = 最后活动水位（随轮询推进）
        end = toMs(a.last_activity_at) ?? t1;
      } else if (a.duration_ms != null) {
        end = start + a.duration_ms;
      } else {
        end = toMs(a.last_activity_at) ?? start;
      }
      const s = Math.min(Math.max(start, t0), t1);
      const e = Math.min(Math.max(end, s), t1);
      return {
        agent: a,
        leftPct: ((s - t0) / span) * 100,
        widthPct: Math.max(((e - s) / span) * 100, 0.75),
        durationMs: a.duration_ms ?? (e > s ? e - s : null),
      };
    });

    // ── 按 phase_index 分组：>0 升序在前，0/缺失 =「相位待定」置底 ──
    const byPhase = new Map<number, SwimLaneRow[]>();
    for (const r of rows) {
      const idx = r.agent.phase_index ?? 0;
      const bucket = byPhase.get(idx);
      if (bucket) bucket.push(r);
      else byPhase.set(idx, [r]);
    }
    const phaseTitle = (idx: number, groupRows: SwimLaneRow[]): string => {
      if (idx === 0) return t.workflows.phasePending;
      return (
        groupRows.find((r) => r.agent.phase_title)?.agent.phase_title ||
        (run.phases ?? []).find((p) => p.index === idx)?.title ||
        `Phase ${idx}`
      );
    };
    const groups: SwimLaneGroup[] = [...byPhase.entries()]
      .sort(([a], [b]) => (a === 0 ? 1 : b === 0 ? -1 : a - b))
      .map(([idx, groupRows]) => ({
        phaseIndex: idx,
        title: phaseTitle(idx, groupRows),
        rows: groupRows.sort(
          (x, y) =>
            (toMs(x.agent.started_at) ?? Number.MAX_SAFE_INTEGER) -
            (toMs(y.agent.started_at) ?? Number.MAX_SAFE_INTEGER),
        ),
      }));

    return { groups, span };
  }, [run, agents, live, t]);

  if (!model) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">{t.workflows.noAgents}</p>
    );
  }

  return (
    <div className="space-y-4">
      {model.groups.map((g) => (
        <div key={g.phaseIndex} className="space-y-1.5">
          <p
            className={cn(
              'text-xs font-medium',
              g.phaseIndex === 0 ? 'text-muted-foreground/70 italic' : 'text-muted-foreground',
            )}
          >
            {g.title}
          </p>
          {g.rows.map((r) => {
            const label = r.agent.label || r.agent.cc_agent_id.slice(0, 8);
            return (
              <div key={r.agent.id || r.agent.cc_agent_id} className="flex items-center gap-2">
                <span
                  className="w-36 shrink-0 truncate text-right text-[11px] text-muted-foreground"
                  title={label}
                >
                  {label}
                </span>
                <div className="relative h-4 flex-1 overflow-hidden rounded bg-muted/40">
                  <div
                    data-swimlane-bar
                    className={cn('absolute top-0 h-full rounded', agentBarClass(r.agent.state))}
                    style={{
                      left: `${r.leftPct}%`,
                      width: `${r.widthPct}%`,
                      minWidth: '0.75%',
                    }}
                    title={`${label} · ${fmtTokens(r.agent.tokens)} · ${fmtDuration(r.durationMs)}`}
                  />
                </div>
              </div>
            );
          })}
        </div>
      ))}
      {/* 底部时间刻度：0/25/50/75/100% */}
      <div className="flex items-center gap-2">
        <span className="w-36 shrink-0" />
        <div className="flex flex-1 justify-between text-[10px] tabular-nums text-muted-foreground/70">
          {[0, 0.25, 0.5, 0.75, 1].map((f) => (
            <span key={f}>{fmtDuration(Math.round(model.span * f))}</span>
          ))}
        </div>
      </div>
    </div>
  );
}

export function WorkflowDetailPage() {
  const t = useT();
  const { wfId } = useParams<{ wfId: string }>();
  const { data: run, isLoading, error } = useWorkflow(wfId ?? '');
  // live run（running/interrupted）时 agents 挂 15s 轮询，泳道 bar 随水位自然推进
  const { data: agentsData } = useWorkflowAgents(
    wfId ?? '',
    run?.status === 'running' || run?.status === 'interrupted',
  );
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
          value={
            isLiveStatus(run.status) ? (
              <span title={t.workflows.liveApprox}>≈{fmtTokens(run.live_tokens)}</span>
            ) : (
              fmtTokens(run.total_tokens)
            )
          }
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
            {run.last_activity_at && (
              <span>
                {t.workflows.lastActivity}: {new Date(run.last_activity_at).toLocaleString()}
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

      {/* 相位泳道（Phase 2：phaseIndex 分组，每 agent 一条 bar） */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            {t.workflows.swimLane}
            {isLiveStatus(run.status) && (
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />
            )}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <PhaseSwimLane run={run} agents={agents} />
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
