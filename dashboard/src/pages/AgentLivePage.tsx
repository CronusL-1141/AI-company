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
import { agentKindLabel, isFreshWorking, readableAgentName } from '@/lib/agentPresentation';

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

  const refreshedAt = Math.max(0, ...statusQueries.map((query) => query.dataUpdatedAt ?? 0));
  return { agents, teams: activeTeams, isLoading, error, refreshedAt };
}

const STATUS_ORDER: Record<AgentStatus, number> = { busy: 0, waiting: 1, offline: 2 };

// Current work includes only busy agents. Waiting and historical rows stay folded.
function partitionAgents(agents: Agent[]) {
  const now = Date.now();
  const sorted = [...agents].sort((a, b) => {
    const byStatus = STATUS_ORDER[resolveStatus(a)] - STATUS_ORDER[resolveStatus(b)];
    if (byStatus !== 0) return byStatus;
    return lastActiveMs(b) - lastActiveMs(a);
  });
  const active: Agent[] = [];
  const dormant: Agent[] = [];
  for (const agent of sorted) {
    if (isFreshWorking(agent, now)) {
      active.push(agent);
      continue;
    }
    dormant.push(agent);
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
          <CardTitle className="truncate text-base font-semibold" title={agent.name}>
            {readableAgentName(agent, t.agentLive.sessionLeader, t.agentLive.unnamedAgent)}
          </CardTitle>
          <p className="mt-0.5 truncate text-sm text-muted-foreground">{t.agentLive.roleTemplate}: {agent.role}</p>
          <p className="text-xs text-muted-foreground">
            {agentKindLabel(agent, t.agentLive.harnessUnknown)}
          </p>
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
  const { agents, teams, isLoading, error, refreshedAt } = useAllAgents(selectedProject);

  const [showDormant, setShowDormant] = useState(false);
  const { active: activeAgents, dormant: dormantAgents } = useMemo(
    () => partitionAgents(agents),
    [agents, refreshedAt],
  );
  const activeGroups = useMemo(() => {
    const groups = new Map<string, Agent[]>();
    for (const agent of activeAgents) {
      groups.set(agent.team_id, [...(groups.get(agent.team_id) ?? []), agent]);
    }
    return [...groups].map(([teamId, members]) => ({
      teamId, name: teams.find((team) => team.id === teamId)?.name ?? teamId,
      members: [...members].sort((a, b) => Number(b.role === 'leader') - Number(a.role === 'leader')),
    }));
  }, [activeAgents, teams]);
  const leaderCount = activeAgents.filter((agent) => agent.role === 'leader').length;

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
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <StatCard
          icon={Activity}
          label={t.agentLive.statBusyLeaders}
          value={leaderCount}
          colorClass="text-green-500"
        />
        <StatCard
          icon={Users}
          label={t.agentLive.statBusyMembers}
          value={activeAgents.length - leaderCount}
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
          {activeGroups.length === 0 && <p className="text-sm text-muted-foreground">{t.agentLive.noWorkingAgents}</p>}
          {activeGroups.map((group) => (
            <section key={group.teamId} className="space-y-2" data-team-id={group.teamId}>
              <h2 className="text-sm font-medium">
                {group.members[0]?.role === 'leader'
                  ? [...new Set([agentKindLabel(group.members[0], t.agentLive.harnessUnknown), readableAgentName(group.members[0], t.agentLive.sessionLeader, t.agentLive.unnamedAgent)])].join(' · ')
                  : group.name}
              </h2>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                {group.members.map((agent) => <AgentCard key={agent.id} agent={agent} />)}
              </div>
            </section>
          ))}

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
