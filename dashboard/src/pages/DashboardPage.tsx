import { Link } from 'react-router-dom';
import { useQueries } from '@tanstack/react-query';
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
import { Users, Bot, ListTodo, CheckCircle, ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useTeams } from '@/api/teams';
import { apiFetch } from '@/api/client';
import type { Team, TeamStatus, APIResponse } from '@/types';

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

function StatusBadge({ status }: { status: string }) {
  const variant =
    status === 'completed'
      ? 'default'
      : status === 'running' || status === 'in_progress'
        ? 'secondary'
        : status === 'failed'
          ? 'destructive'
          : 'outline';
  const label =
    status === 'completed'
      ? '已完成'
      : status === 'running' || status === 'in_progress'
        ? '进行中'
        : status === 'failed'
          ? '失败'
          : status === 'pending'
            ? '等待中'
            : status;
  return <Badge variant={variant}>{label}</Badge>;
}

function TeamOverviewCard({ team, status, loading }: { team: Team; status?: TeamStatus; loading: boolean }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base">{team.name}</CardTitle>
          <Badge variant="secondary">{team.mode}</Badge>
        </div>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-4 w-24" />
        ) : (
          <p className="text-sm text-muted-foreground">
            {status?.agents.length ?? 0} 个 Agent · {status?.total_tasks ?? 0} 个任务
          </p>
        )}
        <Button
          variant="ghost"
          size="sm"
          className="mt-2 -ml-2"
          render={<Link to={`/teams/${team.id}`} />}
        >
          查看详情
          <ArrowRight className="ml-1 h-3 w-3" />
        </Button>
      </CardContent>
    </Card>
  );
}

export function DashboardPage() {
  const { data, isLoading: teamsLoading, error } = useTeams();
  const teams = data?.data ?? [];

  // Batch-fetch all team statuses using useQueries (stable hook count)
  const statusQueries = useQueries({
    queries: teams.map((team) => ({
      queryKey: ['teams', team.id, 'status'],
      queryFn: () => apiFetch<APIResponse<TeamStatus>>(`/api/teams/${team.id}/status`),
      enabled: !!team.id,
    })),
  });

  const statusLoading = statusQueries.some((q) => q.isLoading);

  // Build team→status map
  const statusMap = new Map<string, TeamStatus>();
  teams.forEach((team, i) => {
    const s = statusQueries[i]?.data?.data;
    if (s) statusMap.set(team.id, s);
  });

  // Aggregated stats
  let totalAgents = 0;
  let hookAgents = 0;
  let activeTasks = 0;
  let completedTasks = 0;
  const allTasks: { id: string; title: string; status: string; created_at: string; teamName: string }[] = [];

  for (const [, s] of statusMap) {
    totalAgents += s.agents.length;
    hookAgents += s.agents.filter((a) => a.source === 'hook').length;
    activeTasks += s.active_tasks.length;
    completedTasks += s.completed_tasks;
    allTasks.push(...s.active_tasks.map((t) => ({ ...t, teamName: s.team.name })));
  }

  const recentTasks = allTasks
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .slice(0, 5);

  const allLoaded = teams.length > 0 && statusMap.size === teams.length;

  if (teamsLoading) {
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

  if (error) {
    return (
      <div className="py-12 text-center">
        <p className="text-sm text-destructive">加载失败: {error.message}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Stats Row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title="团队数量"
          value={teams.length}
          description="已创建的团队总数"
          icon={Users}
        />
        <StatCard
          title="Agent 总数"
          value={allLoaded ? totalAgents : '--'}
          description={allLoaded && hookAgents > 0
            ? `含 ${hookAgents} 个自动捕获的 Agent`
            : '所有团队中的 Agent'}
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

      {/* Recent Tasks */}
      <Card>
        <CardHeader>
          <CardTitle>近期任务</CardTitle>
        </CardHeader>
        <CardContent>
          {statusLoading && teams.length > 0 ? (
            <div className="space-y-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : recentTasks.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              暂无近期任务，请在团队详情中执行新任务
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>任务标题</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>团队</TableHead>
                  <TableHead>创建时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {recentTasks.map((task) => (
                  <TableRow key={task.id}>
                    <TableCell className="font-medium">{task.title}</TableCell>
                    <TableCell>
                      <StatusBadge status={task.status} />
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {task.teamName}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {new Date(task.created_at).toLocaleString('zh-CN')}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Team Overview */}
      <Card>
        <CardHeader>
          <CardTitle>团队概览</CardTitle>
        </CardHeader>
        <CardContent>
          {teams.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              暂无团队，请先创建一个团队
            </p>
          ) : (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
              {teams.map((team, i) => (
                <TeamOverviewCard
                  key={team.id}
                  team={team}
                  status={statusMap.get(team.id)}
                  loading={statusQueries[i]?.isLoading ?? false}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
