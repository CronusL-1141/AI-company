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
  MessageSquare,
  ChevronDown,
  ChevronRight,
} from 'lucide-react';
import { useTeam, useTeamStatus } from '@/api/teams';
import { useAgents, useCreateAgent, useDeleteAgent } from '@/api/agents';
import { useRunTask } from '@/api/tasks';
import { useCreateMeeting } from '@/api/meetings';
import { useTeamActivities } from '@/api/activities';
import { LiveIndicator } from '@/components/shared/LiveIndicator';
import { ActivityLog, StatusIcon, formatDuration } from '@/components/agents/ActivityLog';
import { Activity } from 'lucide-react';
import { useT } from '@/i18n';

function StatusBadge({ status }: { status: string }) {
  const t = useT();
  const variant =
    status === 'active' || status === 'online' || status === 'busy'
      ? 'default'
      : status === 'waiting'
        ? 'secondary'
        : status === 'error' || status === 'offline'
          ? 'destructive'
          : 'outline';
  const label =
    status === 'active' || status === 'online'
      ? t.teamDetail.agentStatusOnline
      : status === 'busy'
        ? t.teamDetail.agentStatusBusy
        : status === 'waiting'
          ? t.teamDetail.agentStatusWaiting
          : status === 'error'
            ? t.teamDetail.agentStatusError
            : status === 'offline'
              ? t.teamDetail.agentStatusOffline
              : status;
  return <Badge variant={variant}>{label}</Badge>;
}

function TaskStatusBadge({ status }: { status: string }) {
  const t = useT();
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
      ? t.teamDetail.taskStatusCompleted
      : status === 'running' || status === 'in_progress'
        ? t.teamDetail.taskStatusRunning
        : status === 'failed'
          ? t.teamDetail.taskStatusFailed
          : status === 'pending'
            ? t.teamDetail.taskStatusPending
            : status;
  return <Badge variant={variant}>{label}</Badge>;
}

export function TeamDetailPage() {
  const t = useT();
  const { teamId } = useParams<{ teamId: string }>();
  const { data: teamData, isLoading: teamLoading, error: teamError } = useTeam(teamId ?? '');
  const { data: statusData, isLoading: statusLoading } = useTeamStatus(teamId ?? '');
  const { data: agentsData, isLoading: agentsLoading } = useAgents(teamId ?? '');
  const { data: activitiesData, isLoading: activitiesLoading } = useTeamActivities(teamId ?? '');

  const createAgent = useCreateAgent();
  const deleteAgent = useDeleteAgent();
  const runTask = useRunTask();
  const createMeeting = useCreateMeeting();
  const navigate = useNavigate();

  const team = teamData?.data;
  const status = statusData?.data;
  const agents = useMemo(() => agentsData?.data ?? [], [agentsData?.data]);

  // Sort agents: BUSY > IDLE > OFFLINE
  const sortedAgents = useMemo(() => {
    const priority: Record<string, number> = { busy: 0, active: 0, online: 0, waiting: 1, offline: 2, error: 3 };
    return [...agents].sort(
      (a, b) => (priority[a.status.toLowerCase()] ?? 99) - (priority[b.status.toLowerCase()] ?? 99),
    );
  }, [agents]);

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

  // Expanded Agent (activity log)
  const [expandedAgent, setExpandedAgent] = useState<string | null>(null);

  // Create Meeting Dialog
  const [meetingOpen, setMeetingOpen] = useState(false);
  const [meetingTopic, setMeetingTopic] = useState('');

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
        },
      },
    );
  }

  function handleDeleteAgent() {
    if (!deleteAgentTarget) return;
    deleteAgent.mutate(
      { id: deleteAgentTarget.id, team_id: teamId ?? '' },
      {
        onSuccess: () => {
          setDeleteAgentOpen(false);
          setDeleteAgentTarget(null);
        },
      },
    );
  }

  function handleRunTask(e: React.FormEvent) {
    e.preventDefault();
    if (!teamId || !taskTitle.trim()) return;
    runTask.mutate(
      { team_id: teamId, title: taskTitle.trim(), description: taskDescription.trim() },
      {
        onSuccess: () => {
          setRunTaskOpen(false);
          setTaskTitle('');
          setTaskDescription('');
        },
      },
    );
  }

  function handleCreateMeeting(e: React.FormEvent) {
    e.preventDefault();
    if (!teamId || !meetingTopic.trim()) return;
    createMeeting.mutate(
      { team_id: teamId, topic: meetingTopic.trim() },
      {
        onSuccess: (data) => {
          setMeetingOpen(false);
          setMeetingTopic('');
          if (data.data?.id) navigate(`/meetings/${data.data.id}`);
        },
      },
    );
  }

  if (teamLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-40" />
        <Card>
          <CardContent className="p-6">
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
        <Button variant="ghost" render={<Link to="/projects" />}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          {t.teamDetail.backToList}
        </Button>
        <div className="py-12 text-center">
          <p className="text-sm text-destructive">
            {teamError ? t.teamDetail.loadFailed(teamError.message) : t.teamDetail.notFound}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back Button */}
      <Button variant="ghost" className="-ml-2" render={<Link to="/projects" />}>
        <ArrowLeft className="mr-2 h-4 w-4" />
        {t.teamDetail.backToList}
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
              <p className="text-muted-foreground">{t.teamDetail.colProjectId}</p>
              <p className="font-mono text-xs mt-1">{team.id}</p>
            </div>
            <div>
              <p className="text-muted-foreground">{t.teamDetail.colMode}</p>
              <p className="mt-1">{team.mode}</p>
            </div>
            <div>
              <p className="text-muted-foreground">{t.teamDetail.colAgentCount}</p>
              <p className="mt-1">
                {statusLoading ? <Skeleton className="h-4 w-8 inline-block" /> : (status?.agents.length ?? 0)}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">{t.teamDetail.colCreatedAt}</p>
              <p className="mt-1">{new Date(team.created_at).toLocaleString()}</p>
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
              <CardTitle>{t.teamDetail.agentList}</CardTitle>
            </div>
            <Button size="sm" onClick={() => setAddAgentOpen(true)}>
              <Plus className="mr-1 h-3 w-3" />
              {t.teamDetail.addAgent}
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
              {t.teamDetail.noAgents}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>{t.teamDetail.colName}</TableHead>
                  <TableHead>{t.teamDetail.colRole}</TableHead>
                  <TableHead>{t.teamDetail.colModel}</TableHead>
                  <TableHead>{t.teamDetail.colStatus}</TableHead>
                  <TableHead className="text-right">{t.teamDetail.colActions}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedAgents.map((agent) => (
                  <React.Fragment key={agent.id}>
                    <TableRow
                      className="cursor-pointer hover:bg-muted/50"
                      onClick={() => setExpandedAgent(expandedAgent === agent.id ? null : agent.id)}
                    >
                      <TableCell className="w-8">
                        {expandedAgent === agent.id ? (
                          <ChevronDown className="h-4 w-4 text-muted-foreground" />
                        ) : (
                          <ChevronRight className="h-4 w-4 text-muted-foreground" />
                        )}
                      </TableCell>
                      <TableCell className="font-medium">
                        {agent.name}
                        {agent.source === 'hook' && (
                          <Badge variant="outline" className="ml-2 text-xs bg-yellow-50 text-yellow-700 border-yellow-200">
                            {t.teamDetail.autoCaptured}
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="text-muted-foreground">{agent.role}</TableCell>
                      <TableCell>
                        <Badge variant="outline">{agent.model}</Badge>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <StatusBadge status={agent.status} />
                          {agent.status.toLowerCase() === 'busy' && <LiveIndicator />}
                        </div>
                        {agent.current_task && agent.status.toLowerCase() === 'busy' && (
                          <p className="mt-1 text-xs text-muted-foreground truncate max-w-[300px]" title={agent.current_task}>
                            {agent.current_task}
                          </p>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            setDeleteAgentTarget({ id: agent.id, name: agent.name });
                            setDeleteAgentOpen(true);
                          }}
                        >
                          <Trash2 className="mr-1 h-3 w-3 text-destructive" />
                          {t.teamDetail.deleteAgent}
                        </Button>
                      </TableCell>
                    </TableRow>
                    {expandedAgent === agent.id && (
                      <TableRow>
                        <TableCell colSpan={6} className="bg-muted/30 p-0">
                          <ActivityLog agentId={agent.id} limit={5} />
                        </TableCell>
                      </TableRow>
                    )}
                  </React.Fragment>
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
            <CardTitle>{t.teamDetail.taskHistory}</CardTitle>
            <div className="flex items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => setMeetingOpen(true)}>
                <MessageSquare className="mr-1 h-3 w-3" />
                {t.teamDetail.startMeeting}
              </Button>
              <Button size="sm" onClick={() => setRunTaskOpen(true)}>
                <Play className="mr-1 h-3 w-3" />
                {t.teamDetail.runTask}
              </Button>
            </div>
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
              {t.teamDetail.noTasks}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t.teamDetail.colTaskTitle}</TableHead>
                  <TableHead>{t.teamDetail.colTaskStatus}</TableHead>
                  <TableHead>{t.teamDetail.colTaskCreatedAt}</TableHead>
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
                      {new Date(task.created_at).toLocaleString()}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Activity Tracing */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Activity className="h-5 w-5 text-muted-foreground" />
            <CardTitle>{t.teamDetail.activityTracking}</CardTitle>
            <span className="text-xs text-muted-foreground ml-1">{t.teamDetail.recentCount}</span>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {activitiesLoading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-6 w-full" />
              <Skeleton className="h-6 w-full" />
              <Skeleton className="h-6 w-3/4" />
            </div>
          ) : (activitiesData?.data ?? []).length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">{t.teamDetail.noActivities}</p>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[90px]">{t.teamDetail.colTime}</TableHead>
                    <TableHead className="w-[120px]">Agent</TableHead>
                    <TableHead className="w-[100px]">{t.teamDetail.colTool}</TableHead>
                    <TableHead>{t.teamDetail.colInputSummary}</TableHead>
                    <TableHead className="w-[80px]">{t.teamDetail.colDuration}</TableHead>
                    <TableHead className="w-[60px] text-center">{t.teamDetail.colStatusIcon}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(activitiesData?.data ?? []).map((activity) => (
                    <TableRow key={activity.id} className="text-xs">
                      <TableCell className="font-mono text-muted-foreground py-2">
                        {new Date(activity.timestamp).toLocaleTimeString(undefined, { hour12: false })}
                      </TableCell>
                      <TableCell className="py-2 max-w-[120px]">
                        <span className="truncate block" title={activity.agent_name ?? activity.agent_id}>
                          {activity.agent_name ?? activity.agent_id}
                        </span>
                      </TableCell>
                      <TableCell className="py-2">
                        <Badge variant="outline" className="text-xs font-mono">
                          {activity.tool_name}
                        </Badge>
                      </TableCell>
                      <TableCell className="py-2 max-w-[300px]">
                        <span className="truncate block text-muted-foreground" title={activity.input_summary}>
                          {activity.input_summary || '-'}
                        </span>
                        {activity.error && (
                          <span className="truncate block text-destructive text-xs mt-0.5" title={activity.error}>
                            {activity.error}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="py-2 font-mono text-muted-foreground">
                        {formatDuration(activity.duration_ms)}
                      </TableCell>
                      <TableCell className="py-2 text-center">
                        <StatusIcon status={activity.status} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Add Agent Dialog */}
      <Dialog open={addAgentOpen} onOpenChange={setAddAgentOpen}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={handleCreateAgent}>
            <DialogHeader>
              <DialogTitle>{t.teamDetail.addAgentDialog}</DialogTitle>
              <DialogDescription>
                {t.teamDetail.addAgentDesc(team.name)}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="agent-name">{t.teamDetail.agentNameLabel}</Label>
                <Input
                  id="agent-name"
                  placeholder={t.teamDetail.agentNamePlaceholder}
                  value={agentName}
                  onChange={(e) => setAgentName(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="agent-role">{t.teamDetail.agentRoleLabel}</Label>
                <Input
                  id="agent-role"
                  placeholder={t.teamDetail.agentRolePlaceholder}
                  value={agentRole}
                  onChange={(e) => setAgentRole(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="agent-prompt">{t.teamDetail.agentPromptLabel}</Label>
                <Textarea
                  id="agent-prompt"
                  placeholder={t.teamDetail.agentPromptPlaceholder}
                  value={agentPrompt}
                  onChange={(e) => setAgentPrompt(e.target.value)}
                />
              </div>
              <div className="grid gap-2">
                <Label>{t.teamDetail.agentModelLabel}</Label>
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
                {createAgent.isPending ? t.teamDetail.adding : t.teamDetail.add}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete Agent Dialog */}
      <Dialog open={deleteAgentOpen} onOpenChange={setDeleteAgentOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t.teamDetail.confirmDeleteAgent}</DialogTitle>
            <DialogDescription>
              {t.teamDetail.confirmDeleteAgentDesc(deleteAgentTarget?.name ?? '')}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteAgentOpen(false)}>
              {t.teamDetail.cancel}
            </Button>
            <Button
              variant="destructive"
              onClick={handleDeleteAgent}
              disabled={deleteAgent.isPending}
            >
              {deleteAgent.isPending ? t.teamDetail.deleting : t.teamDetail.confirmDeleteAgent}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Run Task Dialog */}
      <Dialog open={runTaskOpen} onOpenChange={setRunTaskOpen}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={handleRunTask}>
            <DialogHeader>
              <DialogTitle>{t.teamDetail.runTaskDialog}</DialogTitle>
              <DialogDescription>
                {t.teamDetail.runTaskDesc(team.name)}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="task-title">{t.teamDetail.taskTitleLabel}</Label>
                <Input
                  id="task-title"
                  placeholder={t.teamDetail.taskTitlePlaceholder}
                  value={taskTitle}
                  onChange={(e) => setTaskTitle(e.target.value)}
                  required
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="task-desc">{t.teamDetail.taskDescLabel}</Label>
                <Textarea
                  id="task-desc"
                  placeholder={t.teamDetail.taskDescPlaceholder}
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
                {runTask.isPending ? t.teamDetail.running : t.teamDetail.run}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Create Meeting Dialog */}
      <Dialog open={meetingOpen} onOpenChange={setMeetingOpen}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={handleCreateMeeting}>
            <DialogHeader>
              <DialogTitle>{t.teamDetail.meetingDialog}</DialogTitle>
              <DialogDescription>
                {t.teamDetail.meetingDesc(team.name)}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label htmlFor="meeting-topic">{t.teamDetail.meetingTopicLabel}</Label>
                <Input
                  id="meeting-topic"
                  placeholder={t.teamDetail.meetingTopicPlaceholder}
                  value={meetingTopic}
                  onChange={(e) => setMeetingTopic(e.target.value)}
                  required
                />
              </div>
              <div className="text-xs text-muted-foreground">
                {t.teamDetail.meetingParticipants(
                  agents.length > 0 ? agents.map((a) => a.name).join(', ') : t.teamDetail.noParticipants
                )}
              </div>
            </div>
            <DialogFooter>
              <Button
                type="submit"
                disabled={createMeeting.isPending || !meetingTopic.trim() || agents.length === 0}
              >
                {createMeeting.isPending ? t.teamDetail.meetingCreating : t.teamDetail.meetingCreate}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
