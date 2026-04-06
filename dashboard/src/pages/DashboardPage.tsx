import { Link } from 'react-router-dom';
import { useQueries } from '@tanstack/react-query';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Users, Bot, ListTodo, CheckCircle, ArrowRight,
  Activity, BarChart3, Settings, Wifi, WifiOff,
  CircleCheck, CircleX, Clock, AlertTriangle,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useTeams } from '@/api/teams';
import { useProjects } from '@/api/projects';
import { useEvents } from '@/api/events';
import { apiFetch } from '@/api/client';
import { useWSStore } from '@/stores/websocket';
import { useT } from '@/i18n';
import type { Project, TeamStatus, APIResponse, Agent, Task, TaskWallResponse } from '@/types';

function StatCard({
  title,
  value,
  description,
  icon: Icon,
  loading,
}: {
  title: string;
  value: number | string;
  description?: string;
  icon: React.ElementType;
  loading?: boolean;
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-8 w-16" />
        ) : (
          <>
            <p className="text-2xl font-bold">{value}</p>
            {description && (
              <p className="text-xs text-muted-foreground mt-1">{description}</p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** 活跃项目指挥卡片 */
function ActiveProjectCard({
  project,
  status,
}: {
  project: Project;
  status: TeamStatus | undefined;
}) {
  const t = useT();
  const { data: taskWallData } = useQueries({
    queries: [{
      queryKey: ['projects', project.id, 'task-wall'],
      queryFn: () => apiFetch<TaskWallResponse>(`/api/projects/${project.id}/task-wall?include_completed=true&limit=50`),
      enabled: !!project.id,
    }],
  })[0];

  const taskStats = taskWallData?.stats;
  const total = taskStats?.total ?? 0;
  const completed = taskStats?.completed_count ?? 0;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
  const busyAgents = status?.agents.filter((a) => a.status === 'busy').length ?? 0;
  const hasRunning = (status?.active_tasks.length ?? 0) > 0;

  return (
    <Card className={hasRunning ? 'border-primary/40' : ''}>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base truncate">{project.name}</CardTitle>
          {hasRunning && (
            <span className="h-2 w-2 rounded-full bg-green-500 animate-pulse shrink-0" />
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* 进度条 */}
        <div className="space-y-1">
          <div className="flex justify-between text-xs text-muted-foreground">
            <span>{t.dashboard.taskProgress(completed, total)}</span>
            <span>{pct}%</span>
          </div>
          <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
            <div
              className="h-full rounded-full bg-primary transition-all"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
        {/* Agent状态 */}
        <p className="text-xs text-muted-foreground">
          {busyAgents > 0
            ? t.dashboard.agentsWorking(busyAgents)
            : t.dashboard.noAgentActivity}
        </p>
        <Button
          variant="ghost"
          size="sm"
          className="-ml-2"
          render={<Link to={`/projects/${project.id}`} />}
        >
          {t.dashboard.viewDetails}
          <ArrowRight className="ml-1 h-3 w-3" />
        </Button>
      </CardContent>
    </Card>
  );
}

/** 解析event source为可读名称 */
function formatEventSource(
  evt: { source?: string; data?: Record<string, unknown> },
  t: ReturnType<typeof useT>,
): string {
  const name = (evt.data?.name || evt.data?.team_name || evt.data?.topic) as string | undefined;
  if (name) return name;
  const source = evt.source || '';
  if (source.startsWith('team:')) return t.dashboard.teamEvent;
  if (source.startsWith('meeting:')) return t.dashboard.meetingEvent;
  if (source.startsWith('agent:')) return source.split(':')[1]?.substring(0, 8) || source;
  return source;
}

/** Agent状态点 */
function AgentDot({ status }: { status: string }) {
  if (status === 'busy') return <span className="h-2 w-2 rounded-full bg-green-500 inline-block" />;
  if (status === 'waiting') return <span className="h-2 w-2 rounded-full bg-yellow-400 inline-block" />;
  return <span className="h-2 w-2 rounded-full bg-muted-foreground/40 inline-block" />;
}

function agentStatusLabel(status: string, t: ReturnType<typeof useT>) {
  if (status === 'busy') return t.agentStatus.busy;
  if (status === 'waiting') return t.agentStatus.waitingLong;
  return t.agentStatus.offline;
}

const DEPT_LABELS_KEY: Record<string, keyof ReturnType<typeof useT>['projectDetail']> = {
  qa: 'deptQA',
  frontend: 'deptFrontend',
  backend: 'deptBackend',
  'eng-fe': 'deptFrontend',
  'eng-be': 'deptBackend',
  eng: 'deptEng',
  rd: 'deptRD',
  ops: 'deptOps',
  other: 'deptOther',
};

function getDeptPrefix(name: string): string {
  const lower = name.toLowerCase();
  for (const prefix of ['eng-fe', 'eng-be', 'qa', 'frontend', 'backend', 'eng', 'rd', 'ops']) {
    if (lower.startsWith(prefix + '-') || lower === prefix) return prefix;
  }
  return 'other';
}

/** 团队Agent状态概览（按部门分组） */
function TeamAgentOverview({ agents, teamName }: { agents: Agent[]; teamName: string }) {
  const t = useT();
  if (agents.length === 0) return null;

  // 按部门前缀分组
  const groups = new Map<string, Agent[]>();
  for (const agent of agents) {
    const dept = getDeptPrefix(agent.name);
    if (!groups.has(dept)) groups.set(dept, []);
    groups.get(dept)!.push(agent);
  }
  const isGrouped = groups.size > 1;

  function getDeptLabel(dept: string): string {
    const key = DEPT_LABELS_KEY[dept];
    return key ? (t.projectDetail[key] as string) : dept;
  }

  return (
    <div className="space-y-2">
      <p className="text-xs font-medium text-muted-foreground">{teamName}</p>
      {isGrouped ? (
        <div className="space-y-2">
          {Array.from(groups.entries()).map(([dept, deptAgents]) => (
            <div key={dept} className="pl-2 border-l-2 border-muted">
              <p className="text-xs text-muted-foreground/60 mb-1">{getDeptLabel(dept)}</p>
              <div className="flex flex-wrap gap-3">
                {deptAgents.map((agent) => (
                  <div key={agent.id} className="flex items-center gap-1.5 text-sm">
                    <AgentDot status={agent.status} />
                    <span className="font-medium">{agent.name}</span>
                    <span className="text-muted-foreground text-xs">{agentStatusLabel(agent.status, t)}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="flex flex-wrap gap-3">
          {agents.map((agent) => (
            <div key={agent.id} className="flex items-center gap-1.5 text-sm">
              <AgentDot status={agent.status} />
              <span className="font-medium">{agent.name}</span>
              <span className="text-muted-foreground text-xs">{agentStatusLabel(agent.status, t)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** 待处理决策队列 */
function BlockedTaskQueue({ blockedTasks }: { blockedTasks: Array<Task & { teamName: string }> }) {
  const t = useT();
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <AlertTriangle className="h-4 w-4 text-yellow-500" />
          {t.dashboard.pendingDecisions}
          {blockedTasks.length > 0 && (
            <Badge variant="outline" className="ml-1">{blockedTasks.length}</Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {blockedTasks.length === 0 ? (
          <p className="text-sm text-muted-foreground flex items-center gap-2">
            <CircleCheck className="h-4 w-4 text-green-500" />
            {t.dashboard.noPendingItems}
          </p>
        ) : (
          <div className="space-y-2">
            {blockedTasks.map((task) => (
              <div
                key={task.id}
                className="flex items-start gap-2 rounded-md border border-yellow-500/30 bg-yellow-500/5 px-3 py-2"
              >
                <AlertTriangle className="h-3.5 w-3.5 text-yellow-500 shrink-0 mt-0.5" />
                <div className="min-w-0">
                  <p className="text-sm font-medium truncate">{task.title}</p>
                  <p className="text-xs text-muted-foreground">{task.teamName}</p>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function DashboardPage() {
  const t = useT();
  const { data: projectsData, isLoading: projectsLoading, error: projectsError } = useProjects();
  const projects = projectsData?.data ?? [];

  const { data, isLoading: teamsLoading, error } = useTeams();
  const teams = data?.data ?? [];

  const wsConnected = useWSStore((s) => s.connected);

  const { data: eventsData, isLoading: eventsLoading } = useEvents({ limit: 5 });
  const recentEvents = eventsData?.data ?? [];

  // Batch-fetch all team statuses
  const statusQueries = useQueries({
    queries: teams.map((team) => ({
      queryKey: ['teams', team.id, 'status'],
      queryFn: () => apiFetch<APIResponse<TeamStatus>>(`/api/teams/${team.id}/status`),
      enabled: !!team.id,
    })),
  });

  const statusLoading = statusQueries.some((q) => q.isLoading);

  const statusMap = new Map<string, TeamStatus>();
  teams.forEach((team, i) => {
    const s = statusQueries[i]?.data?.data;
    if (s) statusMap.set(team.id, s);
  });

  // 聚合统计
  let totalAgents = 0;
  let hookAgents = 0;
  let activeTasks = 0;
  let completedTasks = 0;

  // 所有agent（含团队名）
  const allAgentsWithTeam: Array<{ agent: Agent; teamName: string }> = [];
  // 所有任务（含团队名）
  const allTasksWithTeam: Array<Task & { teamName: string }> = [];

  for (const [, s] of statusMap) {
    totalAgents += s.agents.length;
    hookAgents += s.agents.filter((a) => a.source === 'hook').length;
    activeTasks += s.active_tasks.length;
    completedTasks += s.completed_tasks;
    for (const agent of s.agents) {
      allAgentsWithTeam.push({ agent, teamName: s.team.name });
    }
    for (const task of s.active_tasks) {
      allTasksWithTeam.push({ ...task, teamName: s.team.name });
    }
  }

  // blocked任务 → 待处理决策队列
  const blockedTasks = allTasksWithTeam.filter((t) => t.status === 'blocked');

  // 有活跃任务的项目（用于指挥中心卡片）
  const projectTeamMap = new Map<string, TeamStatus>();
  for (const team of teams) {
    if (team.project_id) {
      const s = statusMap.get(team.id);
      if (s) projectTeamMap.set(team.project_id, s);
    }
  }

  // 活跃团队的agents（用于概览）
  const activeTeamAgents: Array<{ teamName: string; agents: Agent[] }> = [];
  for (const team of teams) {
    if (team.status === 'active') {
      const s = statusMap.get(team.id);
      if (s && s.agents.length > 0) {
        activeTeamAgents.push({ teamName: team.name, agents: s.agents });
      }
    }
  }

  const allLoaded = teams.length > 0 && statusMap.size === teams.length;

  if (teamsLoading || projectsLoading) {
    return (
      <div className="space-y-6">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[1, 2, 3, 4].map((i) => (
            <Card key={i}>
              <CardHeader className="pb-2">
                <Skeleton className="h-4 w-20" />
              </CardHeader>
              <CardContent>
                <Skeleton className="h-8 w-16" />
              </CardContent>
            </Card>
          ))}
        </div>
        <Card>
          <CardHeader><Skeleton className="h-6 w-24" /></CardHeader>
          <CardContent>
            <div className="space-y-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (error || projectsError) {
    return (
      <div className="py-12 text-center">
        <p className="text-sm text-destructive">
          {t.dashboard.loadFailed((error ?? projectsError)?.message ?? '')}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Stats Row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title={t.dashboard.projectCount}
          value={projects.length}
          description={t.dashboard.projectCountDesc}
          icon={Users}
        />
        <StatCard
          title={t.dashboard.agentCount}
          value={allLoaded ? totalAgents : '--'}
          description={allLoaded && hookAgents > 0
            ? t.dashboard.agentCountHookDesc(hookAgents)
            : t.dashboard.agentCountDesc}
          icon={Bot}
          loading={statusLoading && teams.length > 0}
        />
        <StatCard
          title={t.dashboard.activeTasks}
          value={allLoaded ? activeTasks : '--'}
          description={t.dashboard.activeTasksDesc}
          icon={ListTodo}
          loading={statusLoading && teams.length > 0}
        />
        <StatCard
          title={t.dashboard.completedTasks}
          value={allLoaded ? completedTasks : '--'}
          description={t.dashboard.completedTasksDesc}
          icon={CheckCircle}
          loading={statusLoading && teams.length > 0}
        />
      </div>

      {/* 待处理决策 + 系统健康 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <BlockedTaskQueue blockedTasks={blockedTasks} />

        {/* System Health */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">{t.dashboard.systemHealth}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">{t.dashboard.apiStatus}</span>
                {teamsLoading ? (
                  <Skeleton className="h-5 w-14" />
                ) : error ? (
                  <Badge variant="destructive" className="gap-1">
                    <CircleX className="h-3 w-3" /> {t.dashboard.apiOffline}
                  </Badge>
                ) : (
                  <Badge variant="default" className="gap-1">
                    <CircleCheck className="h-3 w-3" /> {t.dashboard.apiOnline}
                  </Badge>
                )}
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">{t.dashboard.wsConnection}</span>
                {wsConnected ? (
                  <Badge variant="default" className="gap-1">
                    <Wifi className="h-3 w-3" /> {t.dashboard.wsConnected}
                  </Badge>
                ) : (
                  <Badge variant="outline" className="gap-1">
                    <WifiOff className="h-3 w-3" /> {t.dashboard.wsDisconnected}
                  </Badge>
                )}
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">{t.dashboard.teamCount}</span>
                <span className="text-sm font-medium">{teams.length}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">{t.dashboard.lastActivity}</span>
                <span className="text-sm font-medium">
                  {recentEvents.length > 0
                    ? new Date(recentEvents[0].timestamp).toLocaleString('zh-CN')
                    : t.dashboard.noActivity}
                </span>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* 团队Agent状态概览 */}
      {activeTeamAgents.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <Bot className="h-4 w-4" />
              {t.dashboard.teamOverview}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {statusLoading ? (
              <div className="space-y-3">
                <Skeleton className="h-6 w-full" />
                <Skeleton className="h-6 w-3/4" />
              </div>
            ) : (
              <div className="space-y-4">
                {activeTeamAgents.map(({ teamName, agents }) => (
                  <TeamAgentOverview
                    key={teamName}
                    teamName={teamName}
                    agents={agents}
                  />
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* 活跃项目卡片区 */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">{t.dashboard.activeProjects}</CardTitle>
            <Button variant="ghost" size="sm" render={<Link to="/projects" />}>
              {t.dashboard.allProjects}
              <ArrowRight className="ml-1 h-3 w-3" />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {projects.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              {t.dashboard.noProjects}
            </p>
          ) : (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
              {projects.map((project) => (
                <ActiveProjectCard
                  key={project.id}
                  project={project}
                  status={projectTeamMap.get(project.id)}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 快速操作 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t.dashboard.quickActions}</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <Button variant="outline" className="justify-start gap-2" render={<Link to="/tasks" />}>
              <ListTodo className="h-4 w-4" />
              {t.dashboard.viewTaskWall}
            </Button>
            <Button variant="outline" className="justify-start gap-2" render={<Link to="/analytics" />}>
              <BarChart3 className="h-4 w-4" />
              {t.dashboard.viewAnalytics}
            </Button>
            <Button variant="outline" className="justify-start gap-2" render={<Link to="/settings" />}>
              <Settings className="h-4 w-4" />
              {t.dashboard.systemSettings}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* 近期活动 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Activity className="h-4 w-4" />
            {t.dashboard.recentActivity}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {eventsLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : recentEvents.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              {t.dashboard.noActivityRecords}
            </p>
          ) : (
            <div className="space-y-2">
              {recentEvents.map((evt) => (
                <div
                  key={evt.id}
                  className="flex items-center justify-between rounded-md border px-3 py-2"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <Badge variant="outline" className="shrink-0 text-xs">
                      {evt.type}
                    </Badge>
                    <span className="text-sm text-muted-foreground truncate">
                      {formatEventSource(evt, t)}
                    </span>
                  </div>
                  <span className="text-xs text-muted-foreground shrink-0 ml-2 flex items-center gap-1">
                    <Clock className="h-3 w-3" />
                    {new Date(evt.timestamp).toLocaleString('zh-CN')}
                  </span>
                </div>
              ))}
              <Button
                variant="ghost"
                size="sm"
                className="w-full mt-1"
                render={<Link to="/events" />}
              >
                {t.dashboard.viewAllEvents}
                <ArrowRight className="ml-1 h-3 w-3" />
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
