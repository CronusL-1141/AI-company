import { useState, useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
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
import { Users, Activity, Clock, Wifi, FolderOpen, ChevronDown, ChevronRight } from 'lucide-react';
import { apiFetch } from '@/api/client';
import { useTeams } from '@/api/teams';
import { useProjects } from '@/api/projects';
import type { Agent, APIResponse, TeamStatus } from '@/types';
import { useT } from '@/i18n';
import { ContextWatermarkBar } from '@/components/shared/ContextWatermarkBar';
import { serverTimeMs } from '@/lib/datetime';

// Aggregate agents across active teams, optionally scoped to a project
function useAllAgents(projectId?: string) {
  const { data: teamsData, isLoading: teamsLoading, error: teamsError } = useTeams();
  const activeTeams = (teamsData?.data ?? []).filter((t) => {
    if (t.status !== 'active') return false;
    if (projectId) return t.project_id === projectId;
    return true;
  });

  const statusQueries = useQueries({
    queries: activeTeams.map((team) => ({
      queryKey: ['teams', team.id, 'status'],
      queryFn: () => apiFetch<APIResponse<TeamStatus>>(`/api/teams/${team.id}/status`),
      refetchInterval: 30_000,
      staleTime: 20_000,
    })),
  });

  const isLoading = teamsLoading || statusQueries.some((q) => q.isLoading);
  const error = teamsError ?? statusQueries.find((q) => q.error)?.error ?? null;

  const agents: Agent[] = useMemo(() => {
    return statusQueries.flatMap((q) => q.data?.data?.agents ?? []);
  }, [statusQueries]);

  return { agents, isLoading, error };
}

const DORMANT_AFTER_MS = 48 * 60 * 60 * 1000;
const STATUS_ORDER: Record<AgentStatus, number> = { busy: 0, waiting: 1, offline: 2 };

// 在跑的排前面，其余按最后活跃倒序；超过 48 小时没动静的收进折叠层。
// 为什么要分层：agents 行是审计留痕不能删，一个长跑 session 会攒出几百条 offline，
// 平铺会把仅有的几个在跑的淹掉（实测 314 条里只有 3 busy 1 waiting）。
function partitionAgents(agents: Agent[], nowMs: number) {
  const sorted = [...agents].sort((a, b) => {
    const byStatus = STATUS_ORDER[resolveStatus(a)] - STATUS_ORDER[resolveStatus(b)];
    if (byStatus !== 0) return byStatus;
    return lastActiveMs(b) - lastActiveMs(a);
  });
  const active: Agent[] = [];
  const dormant: Agent[] = [];
  for (const agent of sorted) {
    // 在跑的永不折叠，哪怕时间戳很旧或缺失
    if (resolveStatus(agent) !== 'offline') {
      active.push(agent);
      continue;
    }
    const ts = lastActiveMs(agent);
    (ts > 0 && nowMs - ts <= DORMANT_AFTER_MS ? active : dormant).push(agent);
  }
  return { active, dormant };
}

function lastActiveMs(agent: Agent): number {
  const ts = agent.last_active_at;
  if (!ts) return 0;
  const ms = serverTimeMs(ts);
  return Number.isFinite(ms) ? ms : 0;
}

function useFormatLastActive() {
  const t = useT();
  return (ts: string | null | undefined): string => {
    if (!ts) return '';
    const diff = Date.now() - serverTimeMs(ts);
    const minutes = Math.floor(diff / 60_000);
    if (minutes < 1) return t.analytics.timeJustNow;
    if (minutes < 60) return t.analytics.timeMinutesAgo(minutes);
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return t.analytics.timeHoursAgo(hours);
    return t.analytics.timeDaysAgo(Math.floor(hours / 24));
  };
}

type AgentStatus = 'busy' | 'waiting' | 'offline';

function resolveStatus(agent: Agent): AgentStatus {
  const s = agent.status?.toLowerCase();
  if (s === 'busy' || s === 'working') return 'busy';
  if (s === 'waiting' || s === 'idle' || s === 'online') return 'waiting';
  return 'offline';
}

interface StatusBadgeProps {
  status: AgentStatus;
}

function StatusBadge({ status }: StatusBadgeProps) {
  const t = useT();
  if (status === 'busy') {
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className="relative flex h-2.5 w-2.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75" />
          <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-green-500" />
        </span>
        <Badge variant="secondary" className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">
          {t.agentStatus.busy}
        </Badge>
      </span>
    );
  }
  if (status === 'waiting') {
    return (
      <Badge variant="secondary" className="bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200">
        {t.agentStatus.waitingLong}
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400">
      {t.agentStatus.offline}
    </Badge>
  );
}

interface StatCardProps {
  icon: React.ElementType;
  label: string;
  value: number;
  colorClass?: string;
}

function StatCard({ icon: Icon, label, value, colorClass = 'text-muted-foreground' }: StatCardProps) {
  return (
    <Card>
      <CardContent className="flex items-center gap-4 p-6">
        <Icon className={`h-8 w-8 shrink-0 ${colorClass}`} />
        <div>
          <p className="text-sm text-muted-foreground">{label}</p>
          <p className="text-2xl font-bold">{value}</p>
        </div>
      </CardContent>
    </Card>
  );
}

interface AgentCardProps {
  agent: Agent;
}

function AgentCard({ agent }: AgentCardProps) {
  const t = useT();
  const formatLastActive = useFormatLastActive();
  const status = resolveStatus(agent);
  const lastActive = agent.last_active_at
    ? formatLastActive(agent.last_active_at)
    : t.agentLive.cardNeverActive;

  return (
    <Card className="transition-shadow hover:shadow-md">
      <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
        <div className="min-w-0 flex-1">
          <CardTitle className="truncate text-base font-semibold">{agent.name}</CardTitle>
          <p className="mt-0.5 truncate text-sm text-muted-foreground">{agent.role}</p>
        </div>
        <div className="ml-3 shrink-0">
          <StatusBadge status={status} />
        </div>
      </CardHeader>
      <CardContent className="space-y-2 pt-2">
        <div>
          <p className="text-xs font-medium text-muted-foreground">{t.agentLive.cardCurrentTask}</p>
          <p className="mt-0.5 line-clamp-2 text-sm">
            {agent.current_task ?? t.agentLive.cardNoTask}
          </p>
        </div>
        <div className="flex items-center gap-1 text-xs text-muted-foreground">
          <Clock className="h-3 w-3" />
          <span>{t.agentLive.cardLastActive}: {lastActive}</span>
        </div>
        <ContextWatermarkBar pct={agent.ctx_pct} tokens={agent.ctx_tokens} />
      </CardContent>
    </Card>
  );
}

export function AgentLivePage() {
  const t = useT();
  const [projectFilter, setProjectFilter] = useState('__all__');
  const { data: projectsData } = useProjects();
  const projects = projectsData?.data ?? [];

  const selectedProject = projectFilter === '__all__' ? undefined : projectFilter;
  const { agents, isLoading, error } = useAllAgents(selectedProject);

  const busyCount = agents.filter((a) => resolveStatus(a) === 'busy').length;
  const waitingCount = agents.filter((a) => resolveStatus(a) === 'waiting').length;
  const offlineCount = agents.filter((a) => resolveStatus(a) === 'offline').length;

  const [showDormant, setShowDormant] = useState(false);
  const { active: activeAgents, dormant: dormantAgents } = useMemo(
    () => partitionAgents(agents, Date.now()),
    [agents],
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t.agentLive.title}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{t.agentLive.subtitle}</p>
        </div>
        <div className="flex items-center gap-3">
          <Select value={projectFilter} onValueChange={(v) => setProjectFilter(v ?? '__all__')}>
            <SelectTrigger className="h-8 w-[180px] text-sm">
              <FolderOpen className="mr-1.5 h-3.5 w-3.5" />
              <SelectValue placeholder={t.common.allProjects} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">{t.common.allProjects}</SelectItem>
              {projects.map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Wifi className="h-3.5 w-3.5" />
            <span>{t.agentLive.autoRefresh}</span>
          </div>
        </div>
      </div>

      {/* Stats bar */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <StatCard icon={Users} label={t.agentLive.statTotal} value={agents.length} />
        <StatCard
          icon={Activity}
          label={t.agentLive.statBusy}
          value={busyCount}
          colorClass="text-green-500"
        />
        <StatCard
          icon={Clock}
          label={t.agentLive.statWaiting}
          value={waitingCount}
          colorClass="text-yellow-500"
        />
        <StatCard
          icon={Users}
          label={t.agentLive.statOffline}
          value={offlineCount}
          colorClass="text-gray-400"
        />
      </div>

      {/* Agent grid */}
      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Card key={i}>
              <CardHeader>
                <Skeleton className="h-5 w-3/4" />
                <Skeleton className="h-4 w-1/2" />
              </CardHeader>
              <CardContent className="space-y-2">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-2/3" />
              </CardContent>
            </Card>
          ))}
        </div>
      ) : error ? (
        <p className="text-sm text-destructive">
          {t.agentLive.loadFailed((error as Error).message)}
        </p>
      ) : agents.length === 0 ? (
        <div className="py-16 text-center">
          <Users className="mx-auto h-12 w-12 text-muted-foreground/40" />
          <p className="mt-4 text-sm font-medium text-muted-foreground">{t.agentLive.noAgents}</p>
          <p className="mt-1 text-xs text-muted-foreground">{t.agentLive.noAgentsHint}</p>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {activeAgents.map((agent) => (
              <AgentCard key={agent.id} agent={agent} />
            ))}
          </div>

          {dormantAgents.length > 0 && (
            <div className="space-y-4 border-t pt-4">
              <button
                type="button"
                onClick={() => setShowDormant((v) => !v)}
                className="flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
              >
                {showDormant ? (
                  <ChevronDown className="h-4 w-4" />
                ) : (
                  <ChevronRight className="h-4 w-4" />
                )}
                <span>{t.agentLive.dormantSection(dormantAgents.length)}</span>
                <span className="text-xs">
                  {showDormant ? t.agentLive.dormantCollapse : t.agentLive.dormantExpand}
                </span>
              </button>
              {showDormant && (
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                  {dormantAgents.map((agent) => (
                    <AgentCard key={agent.id} agent={agent} />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
