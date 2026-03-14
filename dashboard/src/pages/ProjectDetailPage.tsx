import React, { useState, useMemo } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
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
} from 'lucide-react';
import { useProject } from '@/api/projects';
import { useTeams, useTeam, useTeamStatus } from '@/api/teams';
import { useAgents, useCreateAgent, useDeleteAgent } from '@/api/agents';
import { useRunTask } from '@/api/tasks';
import { useCreateMeeting } from '@/api/meetings';
import { LiveIndicator } from '@/components/shared/LiveIndicator';
import type { Team, Agent } from '@/types';

/* ── Status Badges ── */

function AgentStatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase();
  const variant = s === 'busy' ? 'default' : s === 'idle' ? 'secondary' : s === 'offline' ? 'destructive' : 'outline';
  const label = s === 'busy' ? '工作中' : s === 'idle' ? '空闲' : s === 'offline' ? '离线' : status;
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
  const navigate = useNavigate();
  const createAgent = useCreateAgent();
  const deleteAgent = useDeleteAgent();
  const runTask = useRunTask();
  const createMeeting = useCreateMeeting();

  const agents = (agentsData?.data ?? []).filter((a) => a.role !== 'leader');
  const sortedAgents = useMemo(() => {
    const priority: Record<string, number> = { busy: 0, idle: 1, offline: 2 };
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
          <p className="text-sm text-muted-foreground text-center py-6">暂无成员，点击 "+ Agent" 添加</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Agent</TableHead>
                <TableHead>角色</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>当前任务</TableHead>
                <TableHead>来源</TableHead>
                <TableHead className="w-10"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sortedAgents.map((agent) => (
                <TableRow key={agent.id}>
                  <TableCell className="font-medium">
                    <div className="flex items-center gap-2">
                      <Bot className="h-4 w-4 text-muted-foreground" />
                      {agent.name}
                    </div>
                  </TableCell>
                  <TableCell>{agent.role}</TableCell>
                  <TableCell><AgentStatusBadge status={agent.status} /></TableCell>
                  <TableCell className="text-sm text-muted-foreground">{agent.current_task || '--'}</TableCell>
                  <TableCell><Badge variant="outline">{agent.source ?? 'api'}</Badge></TableCell>
                  <TableCell>
                    <Button size="icon" variant="ghost" onClick={() => setDeleteTarget({ id: agent.id, name: agent.name })}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
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
      const ta = a.completed_at ? new Date(a.completed_at).getTime() : 0;
      const tb = b.completed_at ? new Date(b.completed_at).getTime() : 0;
      return tb - ta; // 最近完成的在前
    });

  // 收集所有项目团队的agents用于找Leader
  const allAgentQueries = projectTeams.map((t) => useAgents(t.id));
  const allAgents = allAgentQueries.flatMap((q) => q.data?.data ?? []);

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
