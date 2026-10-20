import { useState, useMemo, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
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
import { BarChart3, Activity, Users, Wrench, TrendingUp, Clock, Target, Zap, FolderOpen } from 'lucide-react';
import { useTeams } from '@/api/teams';
import { useProjects } from '@/api/projects';
import { useT } from '@/i18n';
import {
  useToolUsage,
  useAgentProductivity,
  useActivityTimeline,
  useEfficiencyMetrics,
} from '@/api/analytics';
import { parseServerTime, serverTimeMs } from '@/lib/datetime';

const RANKING_PAGE_SIZE = 20;

const TOOL_COLORS: Record<string, string> = {
  Bash: 'bg-gray-500',
  Read: 'bg-blue-500',
  Edit: 'bg-yellow-500',
  Write: 'bg-green-500',
  Grep: 'bg-purple-500',
  Glob: 'bg-purple-400',
  Agent: 'bg-orange-500',
};

function getToolColor(name: string) {
  return TOOL_COLORS[name] ?? 'bg-primary';
}

export function AnalyticsPage() {
  const t = useT();
  const { data: projectsData, error: projectsError } = useProjects();
  const projects = projectsData?.data ?? [];
  const { data: teamsData, isLoading: teamsLoading, error: teamsError } = useTeams();
  const allTeams = useMemo(() => teamsData?.data ?? [], [teamsData]);

  const [selectedProjectId, setSelectedProjectId] = useState<string>('__all__');
  const [selectedTeamId, setSelectedTeamId] = useState<string>('__all__');
  const [rankingPage, setRankingPage] = useState({ scope: '', offset: 0 });

  // Filter teams by selected project
  const teams = useMemo(() => {
    if (selectedProjectId === '__all__') return allTeams;
    return allTeams.filter((tm) => tm.project_id === selectedProjectId);
  }, [allTeams, selectedProjectId]);

  // Reset team selection when project changes and selected team is no longer in list
  const effectiveTeamId = useMemo(() => {
    if (selectedTeamId === '__all__') return '__all__';
    if (teams.some((tm) => tm.id === selectedTeamId)) return selectedTeamId;
    return '__all__';
  }, [selectedTeamId, teams]);

  const activeTeamId = effectiveTeamId === '__all__' ? undefined : effectiveTeamId;
  const activeProjectId = selectedProjectId === '__all__' ? undefined : selectedProjectId;
  const rankingScope = `${activeProjectId ?? ''}/${activeTeamId ?? ''}`;
  const rankingOffset = rankingPage.scope === rankingScope ? rankingPage.offset : 0;
  const toolQuery = useToolUsage(activeTeamId, activeProjectId);
  const productivityQuery = useAgentProductivity(activeTeamId, activeProjectId, rankingOffset, RANKING_PAGE_SIZE);
  const timelineQuery = useActivityTimeline(activeTeamId, 24, activeProjectId);
  const efficiencyQuery = useEfficiencyMetrics(activeTeamId, activeProjectId);
  const { data: toolUsage } = toolQuery;
  const { data: productivityPage } = productivityQuery;
  const productivity = productivityPage?.data;
  const observedAgents = productivityPage?.total ?? 0;

  // Polling can remove the last page. Return to the nearest existing page.
  useEffect(() => {
    if (productivityPage && rankingOffset >= productivityPage.total && rankingOffset > 0) {
      const offset = Math.floor(Math.max(0, productivityPage.total - 1) / RANKING_PAGE_SIZE) * RANKING_PAGE_SIZE;
      setRankingPage({ scope: rankingScope, offset });
    }
  }, [productivityPage, rankingOffset, rankingScope]);
  const { data: timeline } = timelineQuery;
  const { data: efficiency } = efficiencyQuery;
  const error = teamsError ?? projectsError ?? toolQuery.error ?? productivityQuery.error
    ?? timelineQuery.error ?? efficiencyQuery.error;
  const totalActivities = toolUsage?.reduce((sum, row) => sum + row.count, 0) ?? 0;

  // 找最常用的工具
  const topTool = useMemo(() => {
    if (!toolUsage || toolUsage.length === 0) return '--';
    return toolUsage.reduce((a, b) => (a.count > b.count ? a : b)).tool_name;
  }, [toolUsage]);

  // 计算工具最大值，用于进度条
  const maxToolCount = useMemo(() => {
    if (!toolUsage || toolUsage.length === 0) return 1;
    return Math.max(...toolUsage.map((tool) => tool.count), 1);
  }, [toolUsage]);

  // 计算时间线最大值
  const maxTimelineCount = useMemo(() => {
    if (!timeline || timeline.length === 0) return 1;
    return Math.max(...timeline.map((item) => item.count), 1);
  }, [timeline]);

  function formatRelativeTime(ts: string) {
    const diff = Date.now() - serverTimeMs(ts);
    const minutes = Math.floor(diff / 60_000);
    if (minutes < 1) return t.analytics.timeJustNow;
    if (minutes < 60) return t.analytics.timeMinutesAgo(minutes);
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return t.analytics.timeHoursAgo(hours);
    return t.analytics.timeDaysAgo(Math.floor(hours / 24));
  }

  const isLoading = teamsLoading || toolQuery.isLoading || productivityQuery.isLoading
    || timelineQuery.isLoading || efficiencyQuery.isLoading;

  if (error) {
    return <p role="alert" className="text-sm text-destructive">{t.common.loadFailed(error.message)}</p>;
  }

  return (
    <div className="min-w-0 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">{t.analytics.title}</h1>
        </div>

        <div className="flex items-center gap-2">
          <Select value={selectedProjectId} onValueChange={(v) => { setSelectedProjectId(v ?? '__all__'); setSelectedTeamId('__all__'); setRankingPage({ scope: '', offset: 0 }); }}>
            <SelectTrigger className="h-8 w-[200px] text-sm">
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

          {teamsLoading ? (
            <Skeleton className="h-8 w-40" />
          ) : teams.length > 0 ? (
            <Select
              value={effectiveTeamId}
              onValueChange={(v) => { setSelectedTeamId(v ?? '__all__'); setRankingPage({ scope: '', offset: 0 }); }}
            >
              <SelectTrigger className="w-[220px]">
                <SelectValue placeholder={t.analytics.selectTeam}>
                  {effectiveTeamId === '__all__' ? t.analytics.allTeams : (teams.find((tm) => tm.id === effectiveTeamId)?.name ?? t.analytics.selectTeam)}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">{t.analytics.allTeams}</SelectItem>
                {teams.map((tm) => (
                  <SelectItem key={tm.id} value={tm.id}>
                    {tm.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
        </div>
      </div>

      {/* 无团队时的提示 */}
      {!teamsLoading && teams.length === 0 && (
        <div className="rounded-lg border bg-muted/30 p-12 text-center">
          <p className="text-sm text-muted-foreground">{t.analytics.noTeamData}</p>
        </div>
      )}

      {/* 有团队时显示内容 */}
      {(activeTeamId || effectiveTeamId === '__all__') && (
        <>
          {/* 顶部统计卡片行 */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <Card size="sm">
              <CardHeader className="flex flex-row items-center justify-between pb-1">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  {t.analytics.totalActivities}
                </CardTitle>
                <Activity className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                {isLoading ? (
                  <Skeleton className="h-8 w-20" />
                ) : (
                  <div className="text-2xl font-bold">
                    {totalActivities}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card size="sm">
              <CardHeader className="flex flex-row items-center justify-between pb-1">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  {t.analytics.observedAgents}
                </CardTitle>
                <Users className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                {isLoading ? (
                  <Skeleton className="h-8 w-20" />
                ) : (
                  <div className="text-2xl font-bold">
                    {observedAgents}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card size="sm">
              <CardHeader className="flex flex-row items-center justify-between pb-1">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  {t.analytics.topTool}
                </CardTitle>
                <Wrench className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                {isLoading ? (
                  <Skeleton className="h-8 w-20" />
                ) : (
                  <div className="text-2xl font-bold">{topTool}</div>
                )}
              </CardContent>
            </Card>
          </div>

          {/* 中间两列 */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* 工具使用分布 */}
            <Card>
              <CardHeader>
                <CardTitle>{t.analytics.toolDistribution}</CardTitle>
              </CardHeader>
              <CardContent>
                {!toolUsage || toolUsage.length === 0 ? (
                  <p className="text-sm text-muted-foreground text-center py-4">
                    {t.analytics.noToolData}
                  </p>
                ) : (
                  <div className="space-y-3">
                    {toolUsage.map((item) => (
                      <div key={item.tool_name} className="space-y-1">
                        <div className="flex items-center justify-between text-sm">
                          <span className="font-medium">{item.tool_name}</span>
                          <span className="text-muted-foreground">{item.count}</span>
                        </div>
                        <div className="h-2 rounded-full bg-muted overflow-hidden">
                          <div
                            className={`h-full rounded-full transition-all ${getToolColor(item.tool_name)}`}
                            style={{ width: `${(item.count / maxToolCount) * 100}%` }}
                          />
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>

            {/* Agent活跃度排行 */}
            <Card className="min-w-0">
              <CardHeader>
                <CardTitle>{t.analytics.agentRanking}</CardTitle>
              </CardHeader>
              <CardContent>
                {!productivity || productivity.length === 0 ? (
                  <p className="text-sm text-muted-foreground text-center py-4">
                    {t.analytics.noAgentData}
                  </p>
                ) : (
                  <Table className="table-fixed">
                    <TableHeader>
                      <TableRow>
                        <TableHead className="w-[35%] whitespace-normal">{t.analytics.colAgent}</TableHead>
                        <TableHead className="whitespace-normal text-right">{t.analytics.colActivityCount}</TableHead>
                        <TableHead className="whitespace-normal text-right">{t.analytics.colToolTypes}</TableHead>
                        <TableHead className="whitespace-normal text-right">{t.analytics.colLastActive}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {productivity.map((agent) => (
                        <TableRow key={agent.agent_id}>
                          <TableCell className="overflow-hidden font-medium">
                            <span className="block truncate" title={agent.agent_name}>{agent.agent_name}</span>
                          </TableCell>
                          <TableCell className="text-right">
                            <Badge variant="secondary">{agent.activity_count}</Badge>
                          </TableCell>
                          <TableCell className="text-right">
                            {agent.tools_used}
                          </TableCell>
                          <TableCell className="whitespace-normal text-right text-muted-foreground">
                            {formatRelativeTime(agent.last_active)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
                <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-sm" aria-live="polite">
                  <span className="text-muted-foreground">
                    {observedAgents === 0 ? 0 : rankingOffset + 1}–{Math.min(rankingOffset + (productivity?.length ?? 0), observedAgents)} / {observedAgents}
                  </span>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" disabled={rankingOffset === 0 || productivityQuery.isFetching}
                      onClick={() => setRankingPage({ scope: rankingScope, offset: Math.max(0, rankingOffset - RANKING_PAGE_SIZE) })}>
                      {t.common.previous}
                    </Button>
                    <Button variant="outline" size="sm" disabled={rankingOffset + RANKING_PAGE_SIZE >= observedAgents || productivityQuery.isFetching}
                      onClick={() => setRankingPage({ scope: rankingScope, offset: rankingOffset + RANKING_PAGE_SIZE })}>
                      {t.common.next}
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>

          {/* 活动时间线 */}
          <Card>
            <CardHeader>
              <CardTitle>{t.analytics.activityTimeline}</CardTitle>
            </CardHeader>
            <CardContent>
              {!timeline || timeline.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-4">
                  {t.analytics.noTimelineData}
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <div className="flex gap-1">
                    {timeline.map((item, i) => {
                      const heightPct = maxTimelineCount > 0
                        ? (item.count / maxTimelineCount) * 100
                        : 0;
                      // 小时桶标签由后端给成带 +00:00 的 UTC 串；换算成读者本地
                      // 的钟点再显示，否则整张图会平移一个时区。
                      const bucket = parseServerTime(item.hour);
                      const hour = bucket ? bucket.getHours() : item.hour;
                      return (
                        <div
                          key={i}
                          className="min-w-6 flex-1 flex flex-col items-center gap-1 text-center group"
                          title={t.analytics.timeTooltip(hour, item.count)}
                        >
                          <span className="h-4 w-full truncate text-[10px] leading-4 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity">
                            {item.count}
                          </span>
                          {/* 百分比只参照固定柱图区；数值和标签不参与柱高计算。 */}
                          <div className="flex h-24 w-full items-end">
                            <div
                              className="w-full rounded-t bg-primary/80 hover:bg-primary transition-colors"
                              style={{ height: `${heightPct}%`, minHeight: item.count > 0 ? 2 : 0 }}
                            />
                          </div>
                          <span className="h-4 text-[10px] leading-4 text-muted-foreground">
                            {String(hour).padStart(2, '0')}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          {/* 效率指标区域 */}
          <div className="flex items-center gap-2 pt-4">
            <TrendingUp className="h-5 w-5 text-muted-foreground" />
            <h2 className="text-lg font-semibold">{t.analytics.efficiencyMetrics}</h2>
          </div>

          {/* 效率统计卡片行 */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <Card size="sm">
              <CardHeader className="flex flex-row items-center justify-between pb-1">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  {t.analytics.taskCompletionRate}
                </CardTitle>
                <Target className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                {!efficiency ? (
                  <Skeleton className="h-8 w-20" />
                ) : (
                  <div className="flex items-center gap-3">
                    <div className="text-2xl font-bold">
                      {Math.round(efficiency.task_completion.completion_rate * 100)}%
                    </div>
                    <div className="flex-1">
                      <div className="h-2 rounded-full bg-muted overflow-hidden">
                        <div
                          className="h-full rounded-full bg-green-500 transition-all"
                          style={{ width: `${efficiency.task_completion.completion_rate * 100}%` }}
                        />
                      </div>
                      <p className="text-xs text-muted-foreground mt-1">
                        {efficiency.task_completion.completed_tasks} / {efficiency.task_completion.total_tasks}
                      </p>
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>

            <Card size="sm">
              <CardHeader className="flex flex-row items-center justify-between pb-1">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  {t.analytics.avgCompletionTime}
                </CardTitle>
                <Clock className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                {!efficiency ? (
                  <Skeleton className="h-8 w-20" />
                ) : (
                  <div className="text-2xl font-bold">
                    {efficiency.task_completion.avg_completion_hours != null
                      ? efficiency.task_completion.avg_completion_hours < 1
                        ? t.analytics.timeMinutes(Math.round(efficiency.task_completion.avg_completion_hours * 60))
                        : t.analytics.timeHours(efficiency.task_completion.avg_completion_hours)
                      : '--'}
                  </div>
                )}
              </CardContent>
            </Card>

            <Card size="sm">
              <CardHeader className="flex flex-row items-center justify-between pb-1">
                <CardTitle className="text-sm font-medium text-muted-foreground">
                  {t.analytics.avgToolsPerTask}
                </CardTitle>
                <Zap className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                {!efficiency ? (
                  <Skeleton className="h-8 w-20" />
                ) : (
                  <div className="text-2xl font-bold">
                    {efficiency.avg_tools_per_task ?? '--'}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>

          {/* 最高效Agent排行 */}
          <Card>
            <CardHeader>
              <CardTitle>{t.analytics.topAgents}</CardTitle>
            </CardHeader>
            <CardContent>
              {!efficiency || efficiency.top_agents.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-4">
                  {t.analytics.noEfficiencyData}
                </p>
              ) : (
                <Table className="table-fixed">
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t.analytics.colAgent}</TableHead>
                      <TableHead className="text-right">{t.analytics.colActivityCount}</TableHead>
                      <TableHead className="text-right">{t.analytics.colActiveTime}</TableHead>
                      <TableHead className="text-right">{t.analytics.colEfficiency}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {efficiency.top_agents.map((agent) => (
                      <TableRow key={agent.agent_id}>
                        <TableCell className="overflow-hidden font-medium">
                          <span className="block truncate" title={agent.agent_name}>{agent.agent_name}</span>
                        </TableCell>
                        <TableCell className="text-right">
                          <Badge variant="secondary">{agent.activity_count}</Badge>
                        </TableCell>
                        <TableCell className="text-right text-muted-foreground">
                          {agent.span_hours}h
                        </TableCell>
                        <TableCell className="text-right">
                          <Badge variant="outline">{agent.activities_per_hour}</Badge>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
