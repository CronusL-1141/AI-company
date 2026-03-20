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
  const { data: taskWallData } = useQueries({
    queries: [{
      queryKey: ['projects', project.id, 'task-wall'],
      queryFn: () => apiFetch<TaskWallResponse>(`/api/projects/${project.id}/task-wall`),
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
            <span>{completed}/{total} 任务完成</span>
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
            ? `${busyAgents} 个 Agent 工作中`
            : '暂无 Agent 活动'}
        </p>
        <Button
          variant="ghost"
          size="sm"
          className="-ml-2"
          render={<Link to={`/projects/${project.id}`} />}
        >
          查看详情
          <ArrowRight className="ml-1 h-3 w-3" />
        </Button>
      </CardContent>
    </Card>
  );
}

/** 解析event source为可读名称 */
function formatEventSource(evt: { source?: string; data?: Record<string, unknown> }): string {
  const name = (evt.data?.name || evt.data?.team_name || evt.data?.topic) as string | undefined;
  if (name) return name;
  const source = evt.source || '';
  if (source.startsWith('team:')) return '团队事件';
  if (source.startsWith('meeting:')) return '会议事件';
  if (source.startsWith('agent:')) return source.split(':')[1]?.substring(0, 8) || source;
  return source;
}

/** Agent状态点 */
function AgentDot({ status }: { status: string }) {
  if (status === 'busy') return <span className="h-2 w-2 rounded-full bg-green-500 inline-block" />;
  if (status === 'waiting') return <span className="h-2 w-2 rounded-full bg-yellow-400 inline-block" />;
  return <span className="h-2 w-2 rounded-full bg-muted-foreground/40 inline-block" />;
}

function agentStatusLabel(status: string) {
  if (status === 'busy') return '工作中';
  if (status === 'waiting') return '等待中';
  return '离线';
}

/** 团队Agent状态概览 */
function TeamAgentOverview({ agents, teamName }: { agents: Agent[]; teamName: string }) {
  if (agents.length === 0) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{teamName}</p>
      <div className="flex flex-wrap gap-3">
        {agents.map((agent) => (
          <div key={agent.id} className="flex items-center gap-1.5 text-sm">
            <AgentDot status={agent.status} />
            <span className="font-medium">{agent.name}</span>
            <span className="text-muted-foreground text-xs">{agentStatusLabel(agent.status)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 待处理决策队列 */
function BlockedTaskQueue({ blockedTasks }: { blockedTasks: Array<Task & { teamName: string }> }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <AlertTriangle className="h-4 w-4 text-yellow-500" />
          待处理决策
          {blockedTasks.length > 0 && (
            <Badge variant="outline" className="ml-1">{blockedTasks.length}</Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {blockedTasks.length === 0 ? (
          <p className="text-sm text-muted-foreground flex items-center gap-2">
            <CircleCheck className="h-4 w-4 text-green-500" />
            无需处理的事项
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
  // project → team 映射（通过 project_id）
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
        <p className="text-sm text-destructive">加载失败: {(error ?? projectsError)?.message}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Stats Row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title="项目数量"
          value={projects.length}
          description="已创建的项目总数"
          icon={Users}
        />
        <StatCard
          title="Agent 总数"
          value={allLoaded ? totalAgents : '--'}
          description={allLoaded && hookAgents > 0
            ? `含 ${hookAgents} 个自动捕获的 Agent`
            : '所有项目中的 Agent'}
          icon={Bot}
          loading={statusLoading && teams.length > 0}
        />
        <StatCard
          title="活跃任务"
          value={allLoaded ? activeTasks : '--'}
          description="正在执行中的任务"
          icon={ListTodo}
          loading={statusLoading && teams.length > 0}
        />
        <StatCard
          title="已完成任务"
          value={allLoaded ? completedTasks : '--'}
          description="累计完成的任务数"
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
            <CardTitle className="text-base">系统健康状态</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">API 状态</span>
                {teamsLoading ? (
                  <Skeleton className="h-5 w-14" />
                ) : error ? (
                  <Badge variant="destructive" className="gap-1">
                    <CircleX className="h-3 w-3" /> 离线
                  </Badge>
                ) : (
                  <Badge variant="default" className="gap-1">
                    <CircleCheck className="h-3 w-3" /> 在线
                  </Badge>
                )}
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">WebSocket 连接</span>
                {wsConnected ? (
                  <Badge variant="default" className="gap-1">
                    <Wifi className="h-3 w-3" /> 已连接
                  </Badge>
                ) : (
                  <Badge variant="outline" className="gap-1">
                    <WifiOff className="h-3 w-3" /> 未连接
                  </Badge>
                )}
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">团队数量</span>
                <span className="text-sm font-medium">{teams.length}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">最后活动</span>
                <span className="text-sm font-medium">
                  {recentEvents.length > 0
                    ? new Date(recentEvents[0].timestamp).toLocaleString('zh-CN')
                    : '暂无'}
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
              团队状态概览
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
            <CardTitle className="text-base">活跃项目</CardTitle>
            <Button variant="ghost" size="sm" render={<Link to="/projects" />}>
              全部项目
              <ArrowRight className="ml-1 h-3 w-3" />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {projects.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              暂无项目，请先创建一个项目
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
          <CardTitle className="text-base">快速操作</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <Button variant="outline" className="justify-start gap-2" render={<Link to="/tasks" />}>
              <ListTodo className="h-4 w-4" />
              查看任务墙
            </Button>
            <Button variant="outline" className="justify-start gap-2" render={<Link to="/analytics" />}>
              <BarChart3 className="h-4 w-4" />
              活动分析
            </Button>
            <Button variant="outline" className="justify-start gap-2" render={<Link to="/settings" />}>
              <Settings className="h-4 w-4" />
              系统设置
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* 近期活动 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Activity className="h-4 w-4" />
            近期活动
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
              暂无活动记录
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
                      {formatEventSource(evt)}
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
                查看全部事件
                <ArrowRight className="ml-1 h-3 w-3" />
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
