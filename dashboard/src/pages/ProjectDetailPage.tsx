import { useState, useMemo } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  ArrowLeft,
  Plus,
  Trash2,
  Play,
  Bot,
  Info,
  MessageSquare,
  ChevronDown,
  ChevronRight,
  Crown,
  History,
  Users,
  Clock,
  UserPlus,
  GitBranch,
} from 'lucide-react';
import { useProject } from '@/api/projects';
import { useTeams } from '@/api/teams';
import { useAgents, useCreateAgent, useDeleteAgent } from '@/api/agents';
import { useRunTask } from '@/api/tasks';
import { useCreateMeeting } from '@/api/meetings';
import { useTeamActivities } from '@/api/activities';
import { useDecisions, useAgentIntents } from '@/api/decisions';
import type { DecisionEvent, AgentIntent } from '@/api/decisions';
import { StatusIcon, formatDuration } from '@/components/agents/ActivityLog';
import { LiveIndicator } from '@/components/shared/LiveIndicator';
import { RelativeTime } from '@/components/shared/RelativeTime';
import type { Team, Agent } from '@/types';

/* ── Decision Timeline ── */

function decisionDotClass(type: string): string {
  const t = type.toLowerCase();
  if (t.includes('agent')) return 'bg-green-500';
  if (t.includes('task')) return 'bg-blue-500';
  if (t.includes('meeting')) return 'bg-purple-500';
  if (t.includes('team')) return 'bg-orange-500';
  return 'bg-gray-400';
}

function decisionLabel(event: DecisionEvent): string {
  const t = event.type.toLowerCase();
  const d = event.data;
  if (t.includes('agent_created') || t.includes('agent.created')) {
    return `Agent创建: ${d.name ?? d.agent_name ?? event.source}`;
  }
  if (t.includes('task_assigned') || t.includes('task.assigned')) {
    return `任务分配: ${d.title ?? d.task_title ?? '-'}`;
  }
  if (t.includes('meeting')) {
    return `会议: ${d.topic ?? d.meeting_topic ?? '-'}`;
  }
  if (t.includes('team_created') || t.includes('team.created')) {
    return `团队创建: ${d.name ?? d.team_name ?? event.source}`;
  }
  return `${event.type}: ${event.source}`;
}

function decisionDetail(event: DecisionEvent): string | null {
  const t = event.type.toLowerCase();
  const d = event.data;
  if (t.includes('agent')) return d.role ? `角色: ${d.role}` : null;
  if (t.includes('task')) return d.assigned_to ? `分配给: ${d.assigned_to}` : null;
  if (t.includes('meeting')) {
    const parts = d.participants;
    return parts && Array.isArray(parts) ? `参与者: ${parts.join(', ')}` : null;
  }
  return null;
}

function DecisionNode({ event, isLast }: { event: DecisionEvent; isLast: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const detail = decisionDetail(event);
  const timeStr = new Date(event.timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });

  return (
    <div className="flex gap-3">
      {/* 左侧时间线 */}
      <div className="flex flex-col items-center flex-shrink-0">
        <div className={`h-2.5 w-2.5 rounded-full mt-1 flex-shrink-0 ${decisionDotClass(event.type)}`} />
        {!isLast && <div className="w-px flex-1 bg-border mt-1" />}
      </div>
      {/* 内容 */}
      <div className="pb-3 min-w-0 flex-1">
        <button
          className="w-full text-left flex items-center gap-2 group"
          onClick={() => detail && setExpanded(!expanded)}
          type="button"
        >
          <span className="text-xs text-muted-foreground tabular-nums flex-shrink-0">{timeStr}</span>
          <span className="text-sm font-medium truncate">{decisionLabel(event)}</span>
          {detail && (
            expanded
              ? <ChevronDown className="h-3 w-3 text-muted-foreground flex-shrink-0 ml-auto" />
              : <ChevronRight className="h-3 w-3 text-muted-foreground flex-shrink-0 ml-auto opacity-0 group-hover:opacity-100" />
          )}
        </button>
        {expanded && detail && (
          <p className="text-xs text-muted-foreground mt-1 ml-0 pl-0">{detail}</p>
        )}
      </div>
    </div>
  );
}

function DecisionTimeline({ teamId }: { teamId: string }) {
  const { data, isLoading } = useDecisions(teamId);
  const events = data?.data ?? [];

  return (
    <div className="mt-4 border-t pt-4">
      <h4 className="text-sm font-medium mb-3 flex items-center gap-2">
        <GitBranch className="h-4 w-4" /> 决策时间线
      </h4>
      {isLoading ? (
        <Skeleton className="h-16 w-full" />
      ) : events.length === 0 ? (
        <p className="text-xs text-muted-foreground py-3 text-center">暂无决策记录（团队开始工作后自动显示）</p>
      ) : (
        <div className="max-h-64 overflow-y-auto pr-1">
          {events.map((event, i) => (
            <DecisionNode key={event.id} event={event} isLast={i === events.length - 1} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Status Badges ── */

function AgentStatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase();
  const variant = s === 'busy' ? 'default' : s === 'waiting' ? 'secondary' : s === 'offline' ? 'destructive' : 'outline';
  const label = s === 'busy' ? '工作中' : s === 'waiting' ? '等待' : s === 'offline' ? '关闭' : status;
  return <Badge variant={variant}>{label}</Badge>;
}

function TeamStatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase();
  const variant = s === 'active' ? 'default' : s === 'completed' ? 'secondary' : 'outline';
  const label = s === 'active' ? '进行中' : s === 'completed' ? '已完成' : s === 'archived' ? '已归档' : status;
  return <Badge variant={variant}>{label}</Badge>;
}

/* ── Leader Card ── */

function LeaderCard({ agents }: { agents: Agent[] }) {
  const leader = agents.find((a) => a.role === 'leader' || a.role?.includes('Leader'));
  if (!leader) return null;

  const isActive = leader.status?.toLowerCase() === 'busy';
  return (
    <Card className={isActive ? 'border-green-500/50 bg-green-50/30 dark:bg-green-950/10' : ''}>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-3">
          <Crown className={`h-5 w-5 ${isActive ? 'text-green-600' : 'text-muted-foreground'}`} />
          <CardTitle className="text-base">Leader</CardTitle>
          <AgentStatusBadge status={leader.status} />
          {isActive && <LiveIndicator />}
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
          <div>
            <p className="text-muted-foreground">名称</p>
            <p className="font-medium mt-1">{leader.name}</p>
          </div>
          <div>
            <p className="text-muted-foreground">模型</p>
            <p className="mt-1">{leader.model || '--'}</p>
          </div>
          <div>
            <p className="text-muted-foreground">会话</p>
            <p className="font-mono text-xs mt-1">{leader.session_id ? leader.session_id.slice(0, 8) + '...' : '无活跃会话'}</p>
          </div>
          <div>
            <p className="text-muted-foreground">当前任务</p>
            <p className="mt-1">{leader.current_task || '待分配'}</p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

/* ── Active Team Section ── */

function ActiveTeamContent({ team }: { team: Team }) {
  const { data: agentsData, isLoading } = useAgents(team.id);
  const { data: activitiesData } = useTeamActivities(team.id);
  const { data: intentsData } = useAgentIntents(team.id);
  const activities = activitiesData?.data ?? [];
  const intentMap = useMemo(() => {
    const map = new Map<string, AgentIntent>();
    for (const intent of (intentsData?.data ?? [])) {
      map.set(intent.agent_id, intent);
    }
    return map;
  }, [intentsData]);
  const navigate = useNavigate();
  const createAgent = useCreateAgent();
  const deleteAgent = useDeleteAgent();
  const runTask = useRunTask();
  const createMeeting = useCreateMeeting();

  const agents = (agentsData?.data ?? []).filter((a) => a.role !== 'leader');
  const sortedAgents = useMemo(() => {
    const priority: Record<string, number> = { busy: 0, waiting: 1, offline: 2 };
    return [...agents].sort((a, b) => (priority[a.status.toLowerCase()] ?? 99) - (priority[b.status.toLowerCase()] ?? 99));
  }, [agents]);

  const [addOpen, setAddOpen] = useState(false);
  const [agentName, setAgentName] = useState('');
  const [agentRole, setAgentRole] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [taskOpen, setTaskOpen] = useState(false);
  const [taskTitle, setTaskTitle] = useState('');
  const [taskDesc, setTaskDesc] = useState('');
  const [meetingOpen, setMeetingOpen] = useState(false);
  const [meetingTopic, setMeetingTopic] = useState('');

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Users className="h-5 w-5 text-blue-600" />
            <CardTitle className="text-base">{team.name}</CardTitle>
            <TeamStatusBadge status={team.status} />
            <span className="text-sm text-muted-foreground">{agents.length} 成员</span>
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setAddOpen(true)}>
              <Plus className="mr-1 h-3 w-3" /> Agent
            </Button>
            <Button size="sm" variant="outline" onClick={() => setTaskOpen(true)}>
              <Play className="mr-1 h-3 w-3" /> 任务
            </Button>
            <Button size="sm" variant="outline" onClick={() => setMeetingOpen(true)}>
              <MessageSquare className="mr-1 h-3 w-3" /> 会议
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-20 w-full" />
        ) : agents.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-8">
            <UserPlus className="h-8 w-8 text-muted-foreground/40" />
            <div className="text-center">
              <p className="text-sm font-medium text-muted-foreground">暂无团队成员</p>
              <p className="text-xs text-muted-foreground/70 mt-1">点击上方 "+ Agent" 添加成员开始协作</p>
            </div>
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {sortedAgents.map((agent) => {
              const isBusy = agent.status.toLowerCase() === 'busy';
              return (
                <div
                  key={agent.id}
                  className={`relative rounded-lg border p-3 transition-colors ${
                    isBusy
                      ? 'border-l-4 border-l-green-500 bg-green-50/30 dark:bg-green-950/10'
                      : 'border-l-4 border-l-gray-300 dark:border-l-gray-600'
                  }`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-2 min-w-0">
                      <Bot className={`h-4 w-4 flex-shrink-0 ${isBusy ? 'text-green-600' : 'text-muted-foreground'}`} />
                      <span className="font-medium text-sm truncate">{agent.name}</span>
                    </div>
                    <div className="flex items-center gap-1 flex-shrink-0">
                      <AgentStatusBadge status={agent.status} />
                      {isBusy && <LiveIndicator />}
                      <Button size="icon" variant="ghost" className="h-6 w-6" onClick={() => setDeleteTarget({ id: agent.id, name: agent.name })}>
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </div>
                  </div>
                  <div className="mt-2 space-y-1 text-xs text-muted-foreground">
                    <p><span className="text-muted-foreground/70">角色:</span> {agent.role}</p>
                    <p className="truncate">
                      <span className="text-muted-foreground/70">任务:</span>{' '}
                      {agent.current_task || <span className="italic">待分配</span>}
                    </p>
                    {(() => {
                      const intent = intentMap.get(agent.id);
                      if (!isBusy || !intent?.tool_name) return null;
                      return (
                        <div className="mt-1 rounded bg-green-50/50 dark:bg-green-950/20 px-1.5 py-1 space-y-0.5">
                          <p className="font-medium text-green-700 dark:text-green-400 truncate">
                            {intent.intent_summary}
                          </p>
                          {intent.input_preview && (
                            <p className="truncate text-muted-foreground/80" title={intent.input_preview}>
                              {intent.input_preview}
                            </p>
                          )}
                        </div>
                      );
                    })()}
                    <div className="flex items-center gap-1">
                      <Clock className="h-3 w-3 text-muted-foreground/50" />
                      {agent.last_active_at ? (
                        <RelativeTime date={agent.last_active_at} />
                      ) : (
                        <span className="italic">无活动记录</span>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* 活动追踪 */}
        <div className="mt-4 border-t pt-4">
          <h4 className="text-sm font-medium mb-2 flex items-center gap-2">
            <History className="h-4 w-4" /> 活动追踪
          </h4>
          {activities.length === 0 ? (
            <p className="text-xs text-muted-foreground py-3 text-center">暂无活动记录（Agent开始工作后自动显示）</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-muted-foreground border-b">
                    <th className="text-left py-1 pr-2">时间</th>
                    <th className="text-left py-1 pr-2">Agent</th>
                    <th className="text-left py-1 pr-2">工具</th>
                    <th className="text-left py-1 pr-2">摘要</th>
                    <th className="text-right py-1 pr-2">耗时</th>
                    <th className="text-center py-1">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {activities.slice(0, 20).map((a) => (
                    <tr key={a.id} className="border-b border-muted/30">
                      <td className="py-1 pr-2 whitespace-nowrap">{new Date(a.timestamp).toLocaleTimeString('zh-CN', { hour12: false })}</td>
                      <td className="py-1 pr-2 truncate max-w-[80px]">{a.agent_name ?? a.agent_id}</td>
                      <td className="py-1 pr-2 font-mono">{a.tool_name}</td>
                      <td className="py-1 pr-2 truncate max-w-[200px] text-muted-foreground" title={a.input_summary}>{a.input_summary || '-'}</td>
                      <td className="py-1 pr-2 text-right whitespace-nowrap">{formatDuration(a.duration_ms)}</td>
                      <td className="py-1 text-center"><StatusIcon status={a.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* 决策时间线 */}
        <DecisionTimeline teamId={team.id} />
      </CardContent>

      {/* Add Agent Dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <form onSubmit={(e) => {
            e.preventDefault();
            if (!agentName.trim() || !agentRole.trim()) return;
            createAgent.mutate(
              { team_id: team.id, name: agentName.trim(), role: agentRole.trim() },
              { onSuccess: () => { setAddOpen(false); setAgentName(''); setAgentRole(''); } },
            );
          }}>
            <DialogHeader><DialogTitle>添加 Agent</DialogTitle></DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label>名称</Label>
                <Input value={agentName} onChange={(e) => setAgentName(e.target.value)} required />
              </div>
              <div className="grid gap-2">
                <Label>角色</Label>
                <Input value={agentRole} onChange={(e) => setAgentRole(e.target.value)} required />
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={createAgent.isPending}>{createAgent.isPending ? '添加中...' : '添加'}</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete Agent Dialog */}
      <Dialog open={!!deleteTarget} onOpenChange={() => setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
            <DialogDescription>确定要删除 Agent「{deleteTarget?.name}」吗？</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>取消</Button>
            <Button variant="destructive" disabled={deleteAgent.isPending} onClick={() => {
              if (deleteTarget) deleteAgent.mutate({ id: deleteTarget.id, team_id: team.id }, { onSuccess: () => setDeleteTarget(null) });
            }}>{deleteAgent.isPending ? '删除中...' : '确认删除'}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Run Task Dialog */}
      <Dialog open={taskOpen} onOpenChange={setTaskOpen}>
        <DialogContent>
          <form onSubmit={(e) => {
            e.preventDefault();
            if (!taskTitle.trim()) return;
            runTask.mutate(
              { team_id: team.id, title: taskTitle.trim(), description: taskDesc.trim() },
              { onSuccess: () => { setTaskOpen(false); setTaskTitle(''); setTaskDesc(''); } },
            );
          }}>
            <DialogHeader><DialogTitle>创建任务</DialogTitle></DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label>标题</Label>
                <Input value={taskTitle} onChange={(e) => setTaskTitle(e.target.value)} required />
              </div>
              <div className="grid gap-2">
                <Label>描述</Label>
                <Textarea value={taskDesc} onChange={(e) => setTaskDesc(e.target.value)} />
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={runTask.isPending}>{runTask.isPending ? '创建中...' : '创建'}</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Meeting Dialog */}
      <Dialog open={meetingOpen} onOpenChange={setMeetingOpen}>
        <DialogContent>
          <form onSubmit={(e) => {
            e.preventDefault();
            if (!meetingTopic.trim()) return;
            createMeeting.mutate(
              { team_id: team.id, topic: meetingTopic.trim(), participants: agents.map((a) => a.name) },
              { onSuccess: (data) => { setMeetingOpen(false); setMeetingTopic(''); if (data?.data?.id) navigate(`/meetings/${data.data.id}`); } },
            );
          }}>
            <DialogHeader><DialogTitle>发起会议</DialogTitle></DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label>主题</Label>
                <Input value={meetingTopic} onChange={(e) => setMeetingTopic(e.target.value)} required />
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={createMeeting.isPending}>{createMeeting.isPending ? '创建中...' : '发起'}</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

/* ── Completed Team Row (collapsible) ── */

function CompletedTeamRow({ team }: { team: Team }) {
  const [expanded, setExpanded] = useState(false);
  const { data: agentsData } = useAgents(expanded ? team.id : '');
  const agents = (agentsData?.data ?? []).filter((a) => a.role !== 'leader');

  return (
    <div className="border rounded-lg">
      <button
        className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-muted/50 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        <span className="font-medium text-sm">{team.name}</span>
        <TeamStatusBadge status={team.status} />
        {team.completed_at && (
          <span className="text-xs text-muted-foreground ml-auto">
            {new Date(team.completed_at).toLocaleDateString('zh-CN')}
          </span>
        )}
      </button>
      {expanded && (
        <div className="px-4 pb-3 border-t">
          {team.summary && (
            <p className="text-sm text-muted-foreground py-2">{team.summary}</p>
          )}
          {agents.length > 0 && (
            <div className="text-xs text-muted-foreground space-y-1 pt-1">
              {agents.map((a) => (
                <div key={a.id} className="flex items-center gap-2">
                  <Bot className="h-3 w-3" />
                  <span>{a.name}</span>
                  <span className="text-muted-foreground/60">({a.role})</span>
                </div>
              ))}
            </div>
          )}
          {agents.length === 0 && !team.summary && (
            <p className="text-xs text-muted-foreground py-2">无详细记录</p>
          )}
        </div>
      )}
    </div>
  );
}

/* ── Main Page ── */

export function ProjectDetailPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const { data: projectData, isLoading: projectLoading, error: projectError } = useProject(projectId ?? '');
  const { data: teamsData } = useTeams();

  const project = projectData?.data;
  const allTeams = teamsData?.data ?? [];

  // 项目关联的teams
  const projectTeams = allTeams.filter((t) => t.project_id === projectId);

  // 分类：active vs completed/archived
  const activeTeams = projectTeams.filter((t) => t.status === 'active');
  const completedTeams = projectTeams
    .filter((t) => t.status === 'completed' || t.status === 'archived')
    .sort((a, b) => {
      const ta = new Date(a.created_at).getTime();
      const tb = new Date(b.created_at).getTime();
      return tb - ta; // 最新创建的在前
    });

  // 查找Leader：从第一个有leader_agent_id的团队中获取agents
  const leaderTeamId = projectTeams.find((t) => t.leader_agent_id)?.id ?? projectTeams[0]?.id ?? '';
  const { data: leaderTeamAgents } = useAgents(leaderTeamId);
  const allAgents = leaderTeamAgents?.data ?? [];

  if (projectLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  if (projectError || !project) {
    return (
      <div className="space-y-6">
        <Button variant="ghost" render={<Link to="/projects" />}>
          <ArrowLeft className="mr-2 h-4 w-4" /> 返回项目列表
        </Button>
        <div className="py-12 text-center">
          <p className="text-sm text-destructive">
            {projectError ? `加载失败: ${projectError.message}` : '项目不存在'}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back */}
      <Button variant="ghost" className="-ml-2" render={<Link to="/projects" />}>
        <ArrowLeft className="mr-2 h-4 w-4" /> 返回项目列表
      </Button>

      {/* Project Info */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-3">
            <Info className="h-5 w-5 text-muted-foreground" />
            <CardTitle>{project.name}</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
            <div>
              <p className="text-muted-foreground">描述</p>
              <p className="mt-1">{project.description || '--'}</p>
            </div>
            <div>
              <p className="text-muted-foreground">当前团队</p>
              <p className="mt-1">{activeTeams.length} 个</p>
            </div>
            <div>
              <p className="text-muted-foreground">历史团队</p>
              <p className="mt-1">{completedTeams.length} 个</p>
            </div>
            <div>
              <p className="text-muted-foreground">创建时间</p>
              <p className="mt-1">{new Date(project.created_at).toLocaleDateString('zh-CN')}</p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Leader Status */}
      <LeaderCard agents={allAgents} />

      {/* Active Teams */}
      {activeTeams.length > 0 ? (
        <div className="space-y-4">
          {activeTeams.map((team) => (
            <ActiveTeamContent key={team.id} team={team} />
          ))}
        </div>
      ) : (
        <Card>
          <CardContent className="py-8 text-center">
            <Users className="mx-auto h-8 w-8 text-muted-foreground/50 mb-3" />
            <p className="text-sm text-muted-foreground">
              暂无活跃团队。通过 MCP 的 team_create 工具创建新团队开始工作。
            </p>
          </CardContent>
        </Card>
      )}

      {/* Completed Teams */}
      {completedTeams.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-muted-foreground">
            <History className="h-4 w-4" />
            <h3 className="text-sm font-medium">历史团队 ({completedTeams.length})</h3>
          </div>
          <div className="space-y-2">
            {completedTeams.map((team) => (
              <CompletedTeamRow key={team.id} team={team} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
