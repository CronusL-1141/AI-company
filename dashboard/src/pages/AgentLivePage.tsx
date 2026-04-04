import { useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Users, Activity, Clock, Wifi } from 'lucide-react';
import { apiFetch } from '@/api/client';
import { useTeams } from '@/api/teams';
import type { Agent, APIListResponse, APIResponse, TeamStatus } from '@/types';
import { useT } from '@/i18n';

// Aggregate agents across all active teams
function useAllAgents() {
  const { data: teamsData, isLoading: teamsLoading, error: teamsError } = useTeams();
  const activeTeams = (teamsData?.data ?? []).filter((t) => t.status === 'active');

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

function formatLastActive(ts: string | null | undefined): string {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes}分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}小时前`;
  return `${Math.floor(hours / 24)}天前`;
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
  if (status === 'busy') {
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className="relative flex h-2.5 w-2.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75" />
          <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-green-500" />
        </span>
        <Badge variant="secondary" className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">
          工作中
        </Badge>
      </span>
    );
  }
  if (status === 'waiting') {
    return (
      <Badge variant="secondary" className="bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200">
        等待中
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400">
      离线
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
      </CardContent>
    </Card>
  );
}

export function AgentLivePage() {
  const t = useT();
  const { agents, isLoading, error } = useAllAgents();

  const busyCount = agents.filter((a) => resolveStatus(a) === 'busy').length;
  const waitingCount = agents.filter((a) => resolveStatus(a) === 'waiting').length;
  const offlineCount = agents.filter((a) => resolveStatus(a) === 'offline').length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t.agentLive.title}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{t.agentLive.subtitle}</p>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Wifi className="h-3.5 w-3.5" />
          <span>{t.agentLive.autoRefresh}</span>
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
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {agents.map((agent) => (
            <AgentCard key={agent.id} agent={agent} />
          ))}
        </div>
      )}
    </div>
  );
}
