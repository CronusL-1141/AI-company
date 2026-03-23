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
  Table,
  TableHeader,
  TableHead,
  TableBody,
  TableRow,
  TableCell,
} from '@/components/ui/table';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
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
  MessageCircle,
  CheckSquare,
  Star,
  User,
  Filter,
} from 'lucide-react';
import { useProject } from '@/api/projects';
import { useTeams } from '@/api/teams';
import { useAgents, useCreateAgent, useDeleteAgent } from '@/api/agents';
import { useRunTask } from '@/api/tasks';
import { useCreateMeeting } from '@/api/meetings';
import { useTeamActivities } from '@/api/activities';
import { useDecisions, useAgentIntents } from '@/api/decisions';
import { useEvents } from '@/api/events';
import type { AgentIntent } from '@/api/decisions';
import { StatusIcon, formatDuration } from '@/components/agents/ActivityLog';
import { LiveIndicator } from '@/components/shared/LiveIndicator';
import { RelativeTime } from '@/components/shared/RelativeTime';
import { useT } from '@/i18n';
import type { Team, Agent, AgentActivity } from '@/types';

/* ── Decision Timeline ── */

// Unified event type merging legacy DecisionEvent and new Event shapes
type TimelineEvent = {
  id: string;
  type: string;
  source: string;
  data: Record<string, unknown>;
  timestamp: string;
};

// Icon component per event category
function TimelineIcon({ type }: { type: string }) {
  const t = type.toLowerCase();
  if (t.includes('meeting')) return <MessageCircle className="h-3.5 w-3.5" />;
  if (t.includes('task')) return <CheckSquare className="h-3.5 w-3.5" />;
  if (t.includes('decision')) return <Star className="h-3.5 w-3.5" />;
  return <User className="h-3.5 w-3.5" />;
}

// Dot color by importance/type
function timelineDotClass(type: string): string {
  const t = type.toLowerCase();
  if (t.includes('critical') || t.includes('failed') || t.includes('error')) return 'bg-red-500 text-red-500';
  if (t.includes('decision') || t.includes('high')) return 'bg-orange-400 text-orange-400';
  if (t.includes('task') || t.includes('meeting')) return 'bg-blue-500 text-blue-500';
  if (t.includes('agent') || t.includes('team')) return 'bg-green-500 text-green-500';
  return 'bg-gray-400 text-gray-400';
}

function timelineNodeLabel(event: TimelineEvent): string {
  const type = event.type.toLowerCase();
  const d = event.data;
  if (type.includes('agent.created') || type.includes('agent_created')) {
    return `Agent Created: ${String(d.name ?? d.agent_name ?? event.source)}`;
  }
  if (type.includes('task.status_changed') || type.includes('task_status_changed')) {
    const title = String(d.title ?? d.task_title ?? '-');
    const status = String(d.new_status ?? d.status ?? '');
    return status ? `Task ${status}: ${title}` : `Task Changed: ${title}`;
  }
  if (type.includes('task.assigned') || type.includes('task_assigned')) {
    return `Task Assigned: ${String(d.title ?? d.task_title ?? '-')}`;
  }
  if (type.includes('meeting.concluded') || type.includes('meeting_concluded')) {
    return `Meeting Concluded: ${String(d.topic ?? d.meeting_topic ?? '-')}`;
  }
  if (type.includes('meeting')) {
    return `Meeting: ${String(d.topic ?? d.meeting_topic ?? '-')}`;
  }
  if (type.includes('decision.logged') || type.includes('decision_logged')) {
    return `Decision: ${String(d.title ?? d.summary ?? d.content ?? event.source)}`;
  }
  if (type.includes('team.created') || type.includes('team_created')) {
    return `Team Created: ${String(d.name ?? d.team_name ?? event.source)}`;
  }
  return `${event.type}: ${event.source}`;
}

function timelineNodeDetail(event: TimelineEvent): string | null {
  const type = event.type.toLowerCase();
  const d = event.data;
  if (type.includes('agent')) return d.role ? `Role: ${String(d.role)}` : null;
  if (type.includes('task')) {
    const parts: string[] = [];
    if (d.assigned_to) parts.push(`Assigned to: ${String(d.assigned_to)}`);
    if (d.priority) parts.push(`Priority: ${String(d.priority)}`);
    return parts.length ? parts.join(' · ') : null;
  }
  if (type.includes('meeting')) {
    const parts = d.participants;
    return parts && Array.isArray(parts) ? `Participants: ${(parts as string[]).join(', ')}` : null;
  }
  if (type.includes('decision')) {
    return d.rationale ? String(d.rationale) : d.content ? String(d.content) : null;
  }
  return null;
}

function TimelineNode({ event, isLast }: { event: TimelineEvent; isLast: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const detail = timelineNodeDetail(event);
  const dotClass = timelineDotClass(event.type);
  const timeStr = new Date(event.timestamp).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });

  return (
    <div className="flex gap-3">
      {/* Left timeline rail */}
      <div className="flex flex-col items-center flex-shrink-0">
        <div className={`h-6 w-6 rounded-full flex items-center justify-center flex-shrink-0 mt-0.5 ${dotClass} bg-opacity-15 border border-current`}>
          <TimelineIcon type={event.type} />
        </div>
        {!isLast && <div className="w-px flex-1 bg-border mt-1 mb-1" />}
      </div>
      {/* Content */}
      <div className="pb-3 min-w-0 flex-1">
        <button
          className="w-full text-left flex items-start gap-2 group"
          onClick={() => (detail || Object.keys(event.data).length > 0) && setExpanded(!expanded)}
          type="button"
        >
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground tabular-nums flex-shrink-0">{timeStr}</span>
              <span className="text-xs text-muted-foreground/60 truncate">{event.source}</span>
            </div>
            <span className="text-sm font-medium block truncate mt-0.5">{timelineNodeLabel(event)}</span>
          </div>
          {(detail || Object.keys(event.data).length > 0) && (
            expanded
              ? <ChevronDown className="h-3 w-3 text-muted-foreground flex-shrink-0 mt-1" />
              : <ChevronRight className="h-3 w-3 text-muted-foreground flex-shrink-0 mt-1 opacity-0 group-hover:opacity-100" />
          )}
        </button>
        {expanded && (
          <div className="mt-1 rounded bg-muted/40 px-2 py-1.5 text-xs text-muted-foreground">
            {detail && <p className="mb-1">{detail}</p>}
            <pre className="whitespace-pre-wrap font-mono text-[10px] overflow-auto max-h-32">
              {JSON.stringify(event.data, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}

// Event types to aggregate for the decision timeline
const TIMELINE_EVENT_TYPES = ['meeting.concluded', 'task.status_changed', 'decision.logged'];

function DecisionTimeline({ teamId, teamName }: { teamId: string; teamName: string }) {
  const t = useT();
  // Legacy decisions (agent_created, task_assigned, etc.)
  const { data: decisionsData, isLoading: decisionsLoading } = useDecisions(teamId);
  // Rich events from /api/events filtered by relevant types
  const { data: meetingEventsData } = useEvents({ type: 'meeting', limit: 50 });
  const { data: taskEventsData } = useEvents({ type: 'task', limit: 50 });
  const { data: decisionEventsData } = useEvents({ type: 'decision', limit: 50 });

  const allEvents = useMemo<TimelineEvent[]>(() => {
    const seen = new Set<string>();
    const result: TimelineEvent[] = [];

    // Merge legacy decision events
    for (const ev of (decisionsData?.data ?? [])) {
      if (!seen.has(ev.id)) {
        seen.add(ev.id);
        result.push(ev);
      }
    }

    // Merge rich events filtered to relevant types and scoped to team
    const richSources: (typeof meetingEventsData)[] = [meetingEventsData, taskEventsData, decisionEventsData];
    for (const src of richSources) {
      for (const ev of (src?.data ?? [])) {
        // Only include events relevant to this team
        const evTeamId = String(ev.data?.team_id ?? ev.data?.teamId ?? '');
        if (evTeamId && evTeamId !== teamId) continue;
        const evTeamName = String(ev.data?.team_name ?? ev.data?.teamName ?? '');
        if (evTeamName && teamName && !evTeamName.includes(teamName) && !teamName.includes(evTeamName)) continue;

        // Filter to specific event types
        const matchesType = TIMELINE_EVENT_TYPES.some((t) => ev.type === t);
        if (!matchesType) continue;

        if (!seen.has(ev.id)) {
          seen.add(ev.id);
          result.push(ev);
        }
      }
    }

    // Sort descending by timestamp (newest first)
    return result.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());
  }, [decisionsData, meetingEventsData, taskEventsData, decisionEventsData, teamId, teamName]);

  const isLoading = decisionsLoading;

  return (
    <div className="mt-4 border-t pt-4">
      <h4 className="text-sm font-medium mb-3 flex items-center gap-2">
        <GitBranch className="h-4 w-4" /> {t.projectDetail.decisionTimeline}
      </h4>
      {isLoading ? (
        <Skeleton className="h-16 w-full" />
      ) : allEvents.length === 0 ? (
        <p className="text-xs text-muted-foreground py-3 text-center">{t.projectDetail.noDecisions}</p>
      ) : (
        <div className="max-h-72 overflow-y-auto pr-1">
          {allEvents.map((event, i) => (
            <TimelineNode key={event.id} event={event} isLast={i === allEvents.length - 1} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Activity Table ── */

interface ActivityTableProps {
  activities: AgentActivity[];
  t: ReturnType<typeof useT>;
}

function ActivityTable({ activities, t }: ActivityTableProps) {
  const [agentFilter, setAgentFilter] = useState('__all__');
  const [groupByAgent, setGroupByAgent] = useState(false);

  // Unique agent names for the filter dropdown
  const agentNames = useMemo(() => {
    const names = new Set<string>();
    for (const a of activities) {
      const name = a.agent_name ?? a.agent_id;
      if (name) names.add(name);
    }
    return Array.from(names).sort();
  }, [activities]);

  const filtered = useMemo(() => {
    if (agentFilter === '__all__') return activities;
    return activities.filter((a) => (a.agent_name ?? a.agent_id) === agentFilter);
  }, [activities, agentFilter]);

  // Group rows by agent name when groupByAgent is true
  const groups = useMemo<Map<string, AgentActivity[]>>(() => {
    if (!groupByAgent) {
      const map = new Map<string, AgentActivity[]>();
      map.set('__all__', filtered.slice(0, 50));
      return map;
    }
    const map = new Map<string, AgentActivity[]>();
    for (const a of filtered.slice(0, 100)) {
      const key = a.agent_name ?? a.agent_id;
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(a);
    }
    return map;
  }, [filtered, groupByAgent]);

  return (
    <div className="mt-4 border-t pt-4">
      <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
        <h4 className="text-sm font-medium flex items-center gap-2">
          <History className="h-4 w-4" /> {t.projectDetail.activityTracking}
        </h4>
        <div className="flex items-center gap-2">
          {/* Agent filter */}
          {agentNames.length > 1 && (
            <Select value={agentFilter} onValueChange={(v) => setAgentFilter(v ?? '__all__')}>
              <SelectTrigger className="h-7 text-xs w-[140px]">
                <Filter className="h-3 w-3 mr-1 text-muted-foreground" />
                <SelectValue placeholder="All agents" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">All agents</SelectItem>
                {agentNames.map((name) => (
                  <SelectItem key={name} value={name}>{name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {/* Group by agent toggle */}
          {agentNames.length > 1 && (
            <Button
              size="sm"
              variant={groupByAgent ? 'default' : 'outline'}
              className="h-7 text-xs px-2"
              onClick={() => setGroupByAgent((v) => !v)}
              type="button"
            >
              Group
            </Button>
          )}
        </div>
      </div>

      {activities.length === 0 ? (
        <p className="text-xs text-muted-foreground py-3 text-center">{t.projectDetail.noActivityHint}</p>
      ) : (
        <div className="rounded-md border overflow-hidden">
          <div className="max-h-72 overflow-y-auto">
            <Table>
              <TableHeader className="sticky top-0 bg-background z-10">
                <TableRow>
                  <TableHead className="text-xs py-1.5 h-auto">{t.projectDetail.colTime}</TableHead>
                  <TableHead className="text-xs py-1.5 h-auto">{t.projectDetail.colAgent}</TableHead>
                  <TableHead className="text-xs py-1.5 h-auto">{t.projectDetail.colTool}</TableHead>
                  <TableHead className="text-xs py-1.5 h-auto">{t.projectDetail.colSummary}</TableHead>
                  <TableHead className="text-xs py-1.5 h-auto text-right">{t.projectDetail.colDuration}</TableHead>
                  <TableHead className="text-xs py-1.5 h-auto text-center">{t.projectDetail.colStatus}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {Array.from(groups.entries()).map(([groupKey, rows]) => (
                  <>
                    {/* Group header row when grouping is active */}
                    {groupByAgent && (
                      <TableRow key={`group-${groupKey}`} className="bg-muted/50 hover:bg-muted/50">
                        <TableCell colSpan={6} className="text-xs font-semibold py-1 text-muted-foreground">
                          <Bot className="h-3 w-3 inline mr-1" />
                          {groupKey}
                          <span className="ml-1 font-normal">({rows.length})</span>
                        </TableCell>
                      </TableRow>
                    )}
                    {rows.map((a) => (
                      <ActivityRow key={a.id} activity={a} />
                    ))}
                  </>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </div>
  );
}

function ActivityRow({ activity: a }: { activity: AgentActivity }) {
  return (
    <TableRow className="text-xs">
      <TableCell className="py-1 text-muted-foreground whitespace-nowrap">
        {new Date(a.timestamp).toLocaleTimeString('zh-CN', { hour12: false })}
      </TableCell>
      <TableCell className="py-1 max-w-[80px]">
        <span className="truncate block" title={a.agent_name ?? a.agent_id}>
          {a.agent_name ?? a.agent_id}
        </span>
      </TableCell>
      <TableCell className="py-1 font-mono">{a.tool_name}</TableCell>
      <TableCell className="py-1 max-w-[200px] text-muted-foreground">
        <span className="truncate block" title={a.input_summary}>{a.input_summary || '-'}</span>
      </TableCell>
      <TableCell className="py-1 text-right whitespace-nowrap">{formatDuration(a.duration_ms)}</TableCell>
      <TableCell className="py-1 text-center"><StatusIcon status={a.status} /></TableCell>
    </TableRow>
  );
}

/* ── Status Badges ── */

function AgentStatusBadge({ status }: { status: string }) {
  const t = useT();
  const s = status.toLowerCase();
  const variant = s === 'busy' ? 'default' : s === 'waiting' ? 'secondary' : s === 'offline' ? 'destructive' : 'outline';
  const label = s === 'busy' ? t.agentStatus.busy : s === 'waiting' ? t.agentStatus.waiting : s === 'offline' ? t.agentStatus.offline : status;
  return <Badge variant={variant}>{label}</Badge>;
}

function TeamStatusBadge({ status }: { status: string }) {
  const t = useT();
  const s = status.toLowerCase();
  const variant = s === 'active' ? 'default' : s === 'completed' ? 'secondary' : 'outline';
  const label = s === 'active' ? t.teamStatus.active : s === 'completed' ? t.teamStatus.completed : s === 'archived' ? t.teamStatus.archived : status;
  return <Badge variant={variant}>{label}</Badge>;
}

/* ── Leader Card ── */

function LeaderCard({ agents }: { agents: Agent[] }) {
  const t = useT();
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
            <p className="text-muted-foreground">{t.projectDetail.agentName}</p>
            <p className="font-medium mt-1">{leader.name}</p>
          </div>
          <div>
            <p className="text-muted-foreground">{t.projectDetail.agentModel}</p>
            <p className="mt-1">{leader.model || '--'}</p>
          </div>
          <div>
            <p className="text-muted-foreground">{t.projectDetail.agentSession}</p>
            <p className="font-mono text-xs mt-1">{leader.session_id ? leader.session_id.slice(0, 8) + '...' : t.projectDetail.noActiveSession}</p>
          </div>
          <div>
            <p className="text-muted-foreground">{t.projectDetail.agentCurrentTask}</p>
            <p className="mt-1">{leader.current_task || t.projectDetail.agentPending}</p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

/* ── Active Team Section ── */

function getDept(name: string): string {
  const lower = name.toLowerCase();
  for (const prefix of ['eng-fe', 'eng-be', 'qa', 'frontend', 'backend', 'eng', 'rd', 'ops']) {
    if (lower.startsWith(prefix + '-') || lower === prefix) return prefix;
  }
  return 'other';
}

function ActiveTeamContent({ team }: { team: Team }) {
  const t = useT();
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

  const DEPT_LABELS: Record<string, string> = {
    qa: t.projectDetail.deptQA,
    frontend: t.projectDetail.deptFrontend,
    backend: t.projectDetail.deptBackend,
    'eng-fe': t.projectDetail.deptFrontend,
    'eng-be': t.projectDetail.deptBackend,
    eng: t.projectDetail.deptEng,
    rd: t.projectDetail.deptRD,
    ops: t.projectDetail.deptOps,
    other: t.projectDetail.deptOther,
  };

  const deptGroups = useMemo(() => {
    const groups = new Map<string, Agent[]>();
    for (const agent of sortedAgents) {
      const dept = getDept(agent.name);
      if (!groups.has(dept)) groups.set(dept, []);
      groups.get(dept)!.push(agent);
    }
    return groups;
  }, [sortedAgents]);

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
            <span className="text-sm text-muted-foreground">{t.projectDetail.memberCount(agents.length)}</span>
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setAddOpen(true)}>
              <Plus className="mr-1 h-3 w-3" /> {t.projectDetail.addAgent}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setTaskOpen(true)}>
              <Play className="mr-1 h-3 w-3" /> {t.projectDetail.runTask}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setMeetingOpen(true)}>
              <MessageSquare className="mr-1 h-3 w-3" /> {t.projectDetail.startMeeting}
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
              <p className="text-sm font-medium text-muted-foreground">{t.projectDetail.noMembers}</p>
              <p className="text-xs text-muted-foreground/70 mt-1">{t.projectDetail.noMembersHint}</p>
            </div>
          </div>
        ) : (
          <div className="space-y-5">
            {Array.from(deptGroups.entries()).map(([dept, deptAgents]) => (
              <div key={dept}>
                {deptGroups.size > 1 && (
                  <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">
                    {DEPT_LABELS[dept] ?? dept}
                    <span className="ml-1 font-normal normal-case">({deptAgents.length})</span>
                  </p>
                )}
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  {deptAgents.map((agent) => {
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
                          <p><span className="text-muted-foreground/70">{t.projectDetail.agentRole}</span> {agent.role}</p>
                          <p className="truncate">
                            <span className="text-muted-foreground/70">{t.projectDetail.agentTask}</span>{' '}
                            {agent.current_task || <span className="italic">{t.projectDetail.agentPending}</span>}
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
                              <span className="italic">{t.projectDetail.agentNoActivity}</span>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Activity tracking table */}
        <ActivityTable activities={activities} t={t} />

        {/* Decision timeline */}
        <DecisionTimeline teamId={team.id} teamName={team.name} />
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
            <DialogHeader><DialogTitle>{t.projectDetail.addAgentDialog}</DialogTitle></DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label>{t.projectDetail.agentNameLabel}</Label>
                <Input value={agentName} onChange={(e) => setAgentName(e.target.value)} required />
              </div>
              <div className="grid gap-2">
                <Label>{t.projectDetail.agentRoleLabel}</Label>
                <Input value={agentRole} onChange={(e) => setAgentRole(e.target.value)} required />
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={createAgent.isPending}>
                {createAgent.isPending ? t.common.adding : t.common.add}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete Agent Dialog */}
      <Dialog open={!!deleteTarget} onOpenChange={() => setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t.projectDetail.confirmDeleteAgent}</DialogTitle>
            <DialogDescription>{t.projectDetail.confirmDeleteAgentDesc(deleteTarget?.name ?? '')}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>{t.common.cancel}</Button>
            <Button variant="destructive" disabled={deleteAgent.isPending} onClick={() => {
              if (deleteTarget) deleteAgent.mutate({ id: deleteTarget.id, team_id: team.id }, { onSuccess: () => setDeleteTarget(null) });
            }}>{deleteAgent.isPending ? t.common.deleting : t.common.confirm_delete}</Button>
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
            <DialogHeader><DialogTitle>{t.projectDetail.createTask}</DialogTitle></DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label>{t.projectDetail.taskTitleLabel}</Label>
                <Input value={taskTitle} onChange={(e) => setTaskTitle(e.target.value)} required />
              </div>
              <div className="grid gap-2">
                <Label>{t.projectDetail.taskDescLabel}</Label>
                <Textarea value={taskDesc} onChange={(e) => setTaskDesc(e.target.value)} />
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={runTask.isPending}>
                {runTask.isPending ? t.common.creating : t.common.create}
              </Button>
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
            <DialogHeader><DialogTitle>{t.projectDetail.startMeetingDialog}</DialogTitle></DialogHeader>
            <div className="grid gap-4 py-4">
              <div className="grid gap-2">
                <Label>{t.projectDetail.meetingTopicLabel}</Label>
                <Input value={meetingTopic} onChange={(e) => setMeetingTopic(e.target.value)} required />
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={createMeeting.isPending}>
                {createMeeting.isPending ? t.common.creating : t.projectDetail.initiate}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

/* ── Completed Team Row (collapsible) ── */

function CompletedTeamRow({ team }: { team: Team }) {
  const t = useT();
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
            <p className="text-xs text-muted-foreground py-2">{t.projectDetail.noDetailRecord}</p>
          )}
        </div>
      )}
    </div>
  );
}

/* ── Main Page ── */

export function ProjectDetailPage() {
  const t = useT();
  const { projectId } = useParams<{ projectId: string }>();
  const { data: projectData, isLoading: projectLoading, error: projectError } = useProject(projectId ?? '');
  const { data: teamsData } = useTeams();

  const project = projectData?.data;
  const allTeams = teamsData?.data ?? [];

  const projectTeams = allTeams.filter((tm) => tm.project_id === projectId);
  const activeTeams = projectTeams.filter((tm) => tm.status === 'active');
  const completedTeams = projectTeams
    .filter((tm) => tm.status === 'completed' || tm.status === 'archived')
    .sort((a, b) => {
      const ta = new Date(a.created_at).getTime();
      const tb = new Date(b.created_at).getTime();
      return tb - ta;
    });

  const leaderTeamId = projectTeams.find((tm) => tm.leader_agent_id)?.id ?? projectTeams[0]?.id ?? '';
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
          <ArrowLeft className="mr-2 h-4 w-4" /> {t.projectDetail.backToList}
        </Button>
        <div className="py-12 text-center">
          <p className="text-sm text-destructive">
            {projectError ? t.projectDetail.backToList + ': ' + projectError.message : t.projectDetail.projectNotFound}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back */}
      <Button variant="ghost" className="-ml-2" render={<Link to="/projects" />}>
        <ArrowLeft className="mr-2 h-4 w-4" /> {t.projectDetail.backToList}
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
              <p className="text-muted-foreground">{t.projectDetail.description}</p>
              <p className="mt-1">{project.description || '--'}</p>
            </div>
            <div>
              <p className="text-muted-foreground">{t.projectDetail.activeTeams}</p>
              <p className="mt-1">{activeTeams.length} {t.projectDetail.teamsUnit}</p>
            </div>
            <div>
              <p className="text-muted-foreground">{t.projectDetail.historyTeams}</p>
              <p className="mt-1">{completedTeams.length} {t.projectDetail.teamsUnit}</p>
            </div>
            <div>
              <p className="text-muted-foreground">{t.projectDetail.createdAt}</p>
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
              {t.projectDetail.noActiveTeams}
            </p>
          </CardContent>
        </Card>
      )}

      {/* Completed Teams */}
      {completedTeams.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-muted-foreground">
            <History className="h-4 w-4" />
            <h3 className="text-sm font-medium">{t.projectDetail.historyTeamsTitle(completedTeams.length)}</h3>
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
