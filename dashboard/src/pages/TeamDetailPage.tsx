import { useState } from 'react';
import { useParams, Link } from 'react-router-dom';
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  ArrowLeft,
  Plus,
  Trash2,
  Play,
  Bot,
  Info,
} from 'lucide-react';
import { useTeam, useTeamStatus } from '@/api/teams';
import { useAgents, useCreateAgent, useDeleteAgent } from '@/api/agents';
import { useRunTask } from '@/api/tasks';

function StatusBadge({ status }: { status: string }) {
  const variant =
    status === 'active' || status === 'online'
      ? 'default'
      : status === 'idle'
        ? 'secondary'
        : status === 'error' || status === 'offline'
          ? 'destructive'
          : 'outline';
  const label =
    status === 'active' || status === 'online'
      ? '在线'
      : status === 'idle'
        ? '空闲'
        : status === 'error'
          ? '错误'
          : status === 'offline'
            ? '离线'
            : status;
  return <Badge variant={variant}>{label}</Badge>;
}

function TaskStatusBadge({ status }: { status: string }) {
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

export function TeamDetailPage() {
  const { teamId } = useParams<{ teamId: string }>();
  const { data: teamData, isLoading: teamLoading, error: teamError } = useTeam(teamId ?? '');
  const { data: statusData, isLoading: statusLoading } = useTeamStatus(teamId ?? '');
  const { data: agentsData, isLoading: agentsLoading } = useAgents(teamId ?? '');

  const createAgent = useCreateAgent();
  const deleteAgent = useDeleteAgent();
  const runTask = useRunTask();

  const team = teamData?.data;
  const status = statusData?.data;
  const agents = agentsData?.data ?? [];

  // Add Agent Dialog
  const [addAgentOpen, setAddAgentOpen] = useState(false);
  const [agentName, setAgentName] = useState('');
  const [agentRole, setAgentRole] = useState('');
  const [agentPrompt, setAgentPrompt] = useState('');
  const [agentModel, setAgentModel] = useState('gpt-4');

  // Delete Agent Dialog
  const [deleteAgentOpen, setDeleteAgentOpen] = useState(false);
  const [deleteAgentTarget, setDeleteAgentTarget] = useState<{ id: string; name: string } | null>(null);

  // Run Task Dialog
  const [runTaskOpen, setRunTaskOpen] = useState(false);
  const [taskTitle, setTaskTitle] = useState('');
  const [taskDescription, setTaskDescription] = useState('');

  function handleCreateAgent(e: React.FormEvent) {
    e.preventDefault();
    if (!teamId || !agentName.trim() || !agentRole.trim()) return;
    createAgent.mutate(
      {
        team_id: teamId,
        name: agentName.trim(),
        role: agentRole.trim(),
        system_prompt: agentPrompt.trim() || undefined,
        model: agentModel,
      },
      {
        onSuccess: () => {
          setAddAgentOpen(false);
          setAgentName('');
          setAgentRole('');
          setAgentPrompt('');
          setAgentModel('gpt-4');
        },
      }
    );
  }

  function handleDeleteAgent() {
    if (!teamId || !deleteAgentTarget) return;
    deleteAgent.mutate(
      { id: deleteAgentTarget.id, team_id: teamId },
      {
        onSuccess: () => {
          setDeleteAgentOpen(false);
          setDeleteAgentTarget(null);
        },
      }
    );
  }

  function handleRunTask(e: React.FormEvent) {
    e.preventDefault();
    if (!teamId || !taskTitle.trim()) return;
    runTask.mutate(
      {
        team_id: teamId,
        title: taskTitle.trim(),
        description: taskDescription.trim(),
      },
      {
        onSuccess: () => {
          setRunTaskOpen(false);
          setTaskTitle('');
          setTaskDescription('');
        },
      }
    );
  }

  if (teamLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-48" />
        <Card>
          <CardContent className="pt-6">
            <div className="space-y-3">
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-2/3" />
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (teamError || !team) {
    return (
      <div className="space-y-6">
        <Button variant="ghost" render={<Link to="/teams" />}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          返回团队列表
        </Button>
        <div className="py-12 text-center">
          <p className="text-sm text-destructive">
            {teamError ? `加载失败: ${teamError.message}` : '团队不存在'}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back Button */}
      <Button variant="ghost" className="-ml-2" render={<Link to="/teams" />}>
        <ArrowLeft className="mr-2 h-4 w-4" />
        返回团队列表
      </Button>

      {/* Team Info Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-3">
            <Info className="h-5 w-5 text-muted-foreground" />
            <CardTitle>{team.name}</CardTitle>
            <Badge variant="secondary">{team.mode}</Badge>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
            <div>
              <p className="text-muted-foreground">团队 ID</p>
              <p className="font-mono text-xs mt-1">{team.id}</p>
            </div>
            <div>
              <p className="text-muted-foreground">编排模式</p>
              <p className="mt-1">{team.mode}</p>
            </div>
            <div>
              <p className="text-muted-foreground">Agent 数量</p>
              <p className="mt-1">
                {statusLoading ? <Skeleton className="h-4 w-8 inline-block" /> : (status?.agents.length ?? 0)}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">创建时间</p>
              <p className="mt-1">{new Date(team.created_at).toLocaleString('zh-CN')}</p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Agent List */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Bot className="h-5 w-5 text-muted-foreground" />
              <CardTitle>Agent 列表</CardTitle>
            </div>
            <Button size="sm" onClick={() => setAddAgentOpen(true)}>
              <Plus className="mr-1 h-3 w-3" />
              添加 Agent
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {agentsLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : agents.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              暂无 Agent，点击上方按钮添加
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>角色</TableHead>
                  <TableHead>模型</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {agents.map((agent) => (
                  <TableRow key={agent.id}>
                    <TableCell className="font-medium">{agent.name}</TableCell>
                    <TableCell className="text-muted-foreground">{agent.role}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{agent.model}</Badge>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={agent.status} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setDeleteAgentTarget({ id: agent.id, name: agent.name });
                          setDeleteAgentOpen(true);
                        }}
                      >
                        <Trash2 className="mr-1 h-3 w-3 text-destructive" />
                        删除
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Task History */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>任务历史</CardTitle>
            <Button size="sm" onClick={() => setRunTaskOpen(true)}>
              <Play className="mr-1 h-3 w-3" />
              执行新任务
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {statusLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : (status?.active_tasks.length ?? 0) === 0 && (status?.completed_tasks ?? 0) === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              暂无任务记录
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>任务标题</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>创建时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {status?.active_tasks.map((task) => (
                  <TableRow key={task.id}>
                    <TableCell className="font-medium">{task.title}</TableCell>
                    <TableCell>
                      <TaskStatusBadge status={task.status} />
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

      {/* Add Agent Dialog */}
      <Dialog open={addAgentOpen} onOpenChange={setAddAgentOpen}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={handleCreateAgent}>
            <DialogHeader>
              <DialogTitle>添加 Agent</DialogTitle>
              <DialogDescription>
                为团队「{team.name}」添加一个新的 Agent
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="agent-name">Agent 名称</Label>
                <Input
                  id="agent-name"
                  placeholder="输入 Agent 名称"
                  value={agentName}
                  onChange={(e) => setAgentName(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="agent-role">角色</Label>
                <Input
                  id="agent-role"
                  placeholder="例如：researcher, coder, reviewer"
                  value={agentRole}
                  onChange={(e) => setAgentRole(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="agent-prompt">系统提示词</Label>
                <Textarea
                  id="agent-prompt"
                  placeholder="输入 Agent 的系统提示词（可选）"
                  value={agentPrompt}
                  onChange={(e) => setAgentPrompt(e.target.value)}
                />
              </div>
              <div className="grid gap-2">
                <Label>模型</Label>
                <Select value={agentModel} onValueChange={(v) => v && setAgentModel(v)}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="gpt-4">GPT-4</SelectItem>
                    <SelectItem value="gpt-4o">GPT-4o</SelectItem>
                    <SelectItem value="gpt-3.5-turbo">GPT-3.5 Turbo</SelectItem>
                    <SelectItem value="claude-sonnet-4-20250514">Claude Sonnet</SelectItem>
                    <SelectItem value="claude-haiku-4-5-20251001">Claude Haiku</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <DialogFooter>
              <Button
                type="submit"
                disabled={createAgent.isPending || !agentName.trim() || !agentRole.trim()}
              >
                {createAgent.isPending ? '添加中...' : '添加'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete Agent Dialog */}
      <Dialog open={deleteAgentOpen} onOpenChange={setDeleteAgentOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
            <DialogDescription>
              确定要删除 Agent「{deleteAgentTarget?.name}」吗？此操作不可撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteAgentOpen(false)}>
              取消
            </Button>
            <Button
              variant="destructive"
              onClick={handleDeleteAgent}
              disabled={deleteAgent.isPending}
            >
              {deleteAgent.isPending ? '删除中...' : '确认删除'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Run Task Dialog */}
      <Dialog open={runTaskOpen} onOpenChange={setRunTaskOpen}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={handleRunTask}>
            <DialogHeader>
              <DialogTitle>执行新任务</DialogTitle>
              <DialogDescription>
                为团队「{team.name}」创建并执行一个新任务
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="task-title">任务标题</Label>
                <Input
                  id="task-title"
                  placeholder="输入任务标题"
                  value={taskTitle}
                  onChange={(e) => setTaskTitle(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="task-desc">任务描述</Label>
                <Textarea
                  id="task-desc"
                  placeholder="详细描述任务内容"
                  value={taskDescription}
                  onChange={(e) => setTaskDescription(e.target.value)}
                />
              </div>
            </div>
            <DialogFooter>
              <Button
                type="submit"
                disabled={runTask.isPending || !taskTitle.trim()}
              >
                {runTask.isPending ? '执行中...' : '执行'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
