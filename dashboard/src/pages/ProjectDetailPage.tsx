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
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { EcosystemSettingsPanel } from '@/components/ecosystem/EcosystemSettingsPanel';
import {
  ArrowLeft,
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
  Lock,
  AlertTriangle,
  CheckCircle2,
} from 'lucide-react';
import { useProject, useProjectSummary } from '@/api/projects';
import type { SummaryLeader, SummaryWorktree } from '@/api/projects';
import { useTeams } from '@/api/teams';
import { useWorkflowAgents, useWorkflows } from '@/api/workflows';
import type { WorkflowRun } from '@/api/workflows';
import { TeamDisplayName } from '@/pages/TeamsPage';
import { fmtDuration, StatusBadge as WorkflowStatusBadge } from '@/pages/WorkflowsPage';
import { useAgents, useDeleteAgent } from '@/api/agents';
import { useRunTask } from '@/api/tasks';
import { useCreateMeeting } from '@/api/meetings';
import { useTeamActivities } from '@/api/activities';
import { useDecisions, useAgentIntents } from '@/api/decisions';
import { useEvents } from '@/api/events';
import type { AgentIntent } from '@/api/decisions';
import { StatusIcon, formatDuration } from '@/components/agents/ActivityLog';
import { LiveIndicator } from '@/components/shared/LiveIndicator';
import { RelativeTime } from '@/components/shared/RelativeTime';
import { ContextWatermarkBar } from '@/components/shared/ContextWatermarkBar';
import { useToast } from '@/components/shared/useToast';
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

function LeaderCard({ leaders }: { leaders: SummaryLeader[] | null | undefined }) {
  const t = useT();
  if (!leaders || leaders.length === 0) {
    // 不再整卡隐藏（用户 2026-07-07：cronus 项目页"没有显示 leader 栏"）——
    // 显示空态，让每个项目页结构一致、可解释。
    return (
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center gap-3">
            <Crown className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">Leader</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">{t.projectDetail.noLeaderYet}</p>
        </CardContent>
      </Card>
    );
  }

  // 多会话并行（用户裁定 2026-07-10）：每个 CC session 一条 CEO-<英文名>，并列展示
  const anyActive = leaders.some((l) => l.status?.toLowerCase() === 'busy');
  return (
    <Card className={anyActive ? 'border-green-500/50 bg-green-50/30 dark:bg-green-950/10' : ''}>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-3">
          <Crown className={`h-5 w-5 ${anyActive ? 'text-green-600' : 'text-muted-foreground'}`} />
          <CardTitle className="text-base">
            Leader{leaders.length > 1 ? ` ×${leaders.length}` : ''}
          </CardTitle>
          {anyActive && <LiveIndicator />}
        </div>
      </CardHeader>
      <CardContent className="divide-y divide-border">
        {leaders.map((leader) => {
          const isActive = leader.status?.toLowerCase() === 'busy';
          return (
            <div key={leader.session_id || leader.name} className="py-3 first:pt-0 last:pb-0">
              <div className="grid grid-cols-2 gap-4 text-sm md:grid-cols-5">
                <div>
                  <p className="text-muted-foreground">{t.projectDetail.agentName}</p>
                  <div className="flex items-center gap-2 mt-1">
                    <p className={`font-medium ${isActive ? 'text-green-700 dark:text-green-400' : ''}`}>{leader.name}</p>
                    <AgentStatusBadge status={leader.status} />
                  </div>
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
                <div>
                  <p className="text-muted-foreground">{t.projectDetail.inFlightTasks}</p>
                  <p className="mt-1">{leader.in_flight_tasks ?? 0}</p>
                </div>
              </div>
              <ContextWatermarkBar
                pct={leader.ctx_pct}
                tokens={leader.ctx_tokens}
                className="mt-3 max-w-xs"
              />
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

/* ── Worktree Card ── */

// 脱敏展示：只截取 .claude/worktrees/ 之后的相对片段，不暴露本机绝对路径全貌
// （docs/worktree-governance-design.md §4/(c) 前端一节明确要求）。
function shortenWorktreePath(path: string): string {
  const marker = '.claude/worktrees/';
  const idx = path.indexOf(marker);
  if (idx === -1) return path;
  return path.slice(idx);
}

function WorktreeCard({ worktrees }: { worktrees: SummaryWorktree[] | null | undefined }) {
  const t = useT();
  if (!worktrees || worktrees.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-3">
          <GitBranch className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">
            {t.projectDetail.worktrees}
            {worktrees.length > 1 ? ` ×${worktrees.length}` : ''}
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent className="divide-y divide-border">
        {worktrees.map((wt) => (
          <div key={wt.path} className="flex flex-wrap items-center justify-between gap-2 py-2.5 text-sm first:pt-0 last:pb-0">
            <div className="min-w-0 flex-1">
              <p className="truncate font-mono text-xs" title={wt.path}>
                {shortenWorktreePath(wt.path)}
              </p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {wt.branch ?? t.projectDetail.worktreeDetached} · {wt.head}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              {wt.locked && (
                <Badge variant="outline" className="gap-1 text-xs bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950/30 dark:text-blue-300">
                  <Lock className="h-3 w-3" />
                  {t.projectDetail.worktreeLocked}
                </Badge>
              )}
              {wt.dirty && (
                <Badge variant="outline" className="gap-1 text-xs bg-red-50 text-red-700 border-red-200 dark:bg-red-950/30 dark:text-red-300">
                  <AlertTriangle className="h-3 w-3" />
                  {t.projectDetail.worktreeDirty}
                </Badge>
              )}
              {wt.merged === false && (
                <Badge variant="outline" className="gap-1 text-xs bg-yellow-50 text-yellow-700 border-yellow-200 dark:bg-yellow-950/30 dark:text-yellow-300">
                  <AlertTriangle className="h-3 w-3" />
                  {t.projectDetail.worktreeUnmerged}
                </Badge>
              )}
              {!wt.dirty && wt.merged === true && (
                <Badge variant="outline" className="gap-1 text-xs bg-green-50 text-green-700 border-green-200 dark:bg-green-950/30 dark:text-green-300">
                  <CheckCircle2 className="h-3 w-3" />
                  {t.projectDetail.worktreeClean}
                </Badge>
              )}
            </div>
          </div>
        ))}
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

function ActiveTeamContent({ team, run }: { team: Team; run?: WorkflowRun }) {
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
  const deleteAgent = useDeleteAgent();
  const runTask = useRunTask();
  const createMeeting = useCreateMeeting();
  const { showToast, toastNode } = useToast();

  const agents = (agentsData?.data ?? []).filter((a) => a.role !== 'leader');
  const sortedAgents = useMemo(() => {
    const priority: Record<string, number> = { busy: 0, waiting: 1, offline: 2 };
    return [...agents].sort((a, b) => (priority[a.status.toLowerCase()] ?? 99) - (priority[b.status.toLowerCase()] ?? 99));
  }, [agents]);

  // workflow 团队：成员主名用观测层阶段标签，wf-<ccid> 降级小字
  //（与 CompletedTeamRow/TeamDetailPage 同规则——此前活跃团队区漏接，用户 2026-07-07 实测指出）
  const wfId = teamWfId(team);
  const { data: wfAgents } = useWorkflowAgents(wfId ?? '', true);
  const labelByCc = useMemo(() => {
    const m: Record<string, string> = {};
    for (const wa of wfAgents ?? []) {
      if (wa.cc_agent_id) m[wa.cc_agent_id] = wa.label;
    }
    return m;
  }, [wfAgents]);

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

  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [taskOpen, setTaskOpen] = useState(false);
  const [taskTitle, setTaskTitle] = useState('');
  const [taskDesc, setTaskDesc] = useState('');
  const [meetingOpen, setMeetingOpen] = useState(false);
  const [meetingTopic, setMeetingTopic] = useState('');

  return (
    <Card>
      {toastNode}
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 min-w-0">
            <Users className="h-5 w-5 text-blue-600 shrink-0" />
            <CardTitle className="text-base">
              <TeamDisplayName team={team} />
            </CardTitle>
            {run ? <WorkflowStatusBadge status={run.status} /> : <TeamStatusBadge status={team.status} />}
            <span className="text-sm text-muted-foreground whitespace-nowrap">{t.projectDetail.memberCount(agents.length)}</span>
            {run && (
              <Link
                to={`/workflows/${run.wf_id}`}
                className="text-xs text-primary hover:underline whitespace-nowrap"
              >
                {t.projectDetail.viewSwimlane}
              </Link>
            )}
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setTaskOpen(true)}>
              <Play className="mr-1 h-3 w-3" /> {t.projectDetail.runTask}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setMeetingOpen(true)}>
              <MessageSquare className="mr-1 h-3 w-3" /> {t.projectDetail.startMeeting}
            </Button>
          </div>
        </div>
        {run?.summary && (
          <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{run.summary}</p>
        )}
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
                            {agent.cc_tool_use_id && labelByCc[agent.cc_tool_use_id] ? (
                              <span className="flex flex-col min-w-0 leading-tight">
                                <span className="font-medium text-sm truncate">
                                  {labelByCc[agent.cc_tool_use_id]}
                                </span>
                                <span className="font-mono text-[10px] text-muted-foreground/50 truncate">
                                  {agent.name}
                                </span>
                              </span>
                            ) : (
                              <span className="font-medium text-sm truncate">{agent.name}</span>
                            )}
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
                          <ContextWatermarkBar pct={agent.ctx_pct} tokens={agent.ctx_tokens} />
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
              {
                onSuccess: (res) => {
                  setTaskOpen(false);
                  setTaskTitle('');
                  setTaskDesc('');
                  showToast(res._hint ?? res.message);
                },
              },
            );
          }}>
            <DialogHeader>
              <DialogTitle>{t.projectDetail.createTask}</DialogTitle>
              <DialogDescription>{t.projectDetail.runTaskHint}</DialogDescription>
            </DialogHeader>
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
            <DialogHeader>
              <DialogTitle>{t.projectDetail.startMeetingDialog}</DialogTitle>
              <DialogDescription>{t.projectDetail.meetingHint}</DialogDescription>
            </DialogHeader>
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

/** workflow 团队 → wf_id（兜底队 workflow-session-* 无对应 run，返回 undefined） */
function teamWfId(team: Team): string | undefined {
  return (
    (team.config?.workflow_run_id as string | undefined) ??
    (team.name.startsWith('workflow-') && !team.name.startsWith('workflow-session-')
      ? team.name.replace(/^workflow-/, '')
      : undefined)
  );
}

function CompletedTeamRow({ team, run }: { team: Team; run?: WorkflowRun }) {
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  const { data: agentsData } = useAgents(expanded ? team.id : '');
  const agents = (agentsData?.data ?? []).filter((a) => a.role !== 'leader');

  // workflow 团队：成员主名用观测层阶段标签（cc_tool_use_id↔cc_agent_id 关联），
  // wf-<ccid> 降级小字（与 TeamDetailPage 同规则，用户 2026-07-06 需求）。
  const wfId = teamWfId(team);
  const { data: wfAgents } = useWorkflowAgents(expanded && wfId ? wfId : '');
  const labelByCc: Record<string, string> = {};
  for (const wa of wfAgents ?? []) {
    if (wa.cc_agent_id) labelByCc[wa.cc_agent_id] = wa.label;
  }

  const completedAt = run?.completed_at ?? team.completed_at;
  return (
    <div className="border rounded-lg">
      <div className="w-full flex items-center gap-3 px-4 py-3 hover:bg-muted/50 transition-colors">
        <button
          className="flex flex-1 min-w-0 items-center gap-3 text-left"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
          <span className="font-medium text-sm truncate">
            <TeamDisplayName team={team} />
          </span>
          {/* 方案 A（用户 2026-07-07 拍板）：workflow 团队行内摘要——run 状态/agent 数/耗时 */}
          {run ? <WorkflowStatusBadge status={run.status} /> : <TeamStatusBadge status={team.status} />}
          {run && (
            <span className="text-xs text-muted-foreground tabular-nums whitespace-nowrap">
              {run.agent_count} agents · {fmtDuration(run.duration_ms)}
            </span>
          )}
          {completedAt && (
            <span className="text-xs text-muted-foreground ml-auto whitespace-nowrap">
              {new Date(completedAt).toLocaleString('zh-CN', {
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
              })}
            </span>
          )}
        </button>
        {run && (
          <Link
            to={`/workflows/${run.wf_id}`}
            className="text-xs text-primary hover:underline whitespace-nowrap shrink-0"
          >
            {t.projectDetail.viewSwimlane}
          </Link>
        )}
      </div>
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
                  <span>{(a.cc_tool_use_id && labelByCc[a.cc_tool_use_id]) || a.name}</span>
                  {a.cc_tool_use_id && labelByCc[a.cc_tool_use_id] && (
                    <span className="font-mono text-[10px] text-muted-foreground/50">
                      {a.name}
                    </span>
                  )}
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
  const { data: projSummary } = useProjectSummary(projectId ?? '');
  // 方案 A：本项目 workflow run 按 wf_id 索引供团队行内摘要使用。
  // limit 必须 ≤ 后端 Query(le=200)——曾传 500 触发 422 被前端静默兜底成
  // 空列表，摘要条/泳道链接整体消失（2026-07-08 实录）。
  const { data: projectRuns } = useWorkflows({ project_id: projectId ?? '', limit: 200 });
  const runByWfId: Record<string, WorkflowRun> = {};
  for (const r of projectRuns ?? []) runByWfId[r.wf_id] = r;

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
          <div className="space-y-4 text-sm">
            {/* 描述：占满整行，长文本可读性最优 */}
            <div>
              <p className="text-muted-foreground">{t.projectDetail.description}</p>
              <p className="mt-1 leading-relaxed whitespace-pre-wrap">{project.description || '--'}</p>
            </div>
            {/* 三个统计：紧凑成一排（各占数字宽度），上方细分隔线 */}
            <div className="flex flex-wrap items-baseline gap-x-8 gap-y-2 border-t pt-3">
              <div className="flex items-baseline gap-2">
                <span className="text-muted-foreground">{t.projectDetail.activeTeams}</span>
                <span className="font-medium tabular-nums">{activeTeams.length} {t.projectDetail.teamsUnit}</span>
              </div>
              <div className="flex items-baseline gap-2">
                <span className="text-muted-foreground">{t.projectDetail.historyTeams}</span>
                <span className="font-medium tabular-nums">{completedTeams.length} {t.projectDetail.teamsUnit}</span>
              </div>
              <div className="flex items-baseline gap-2">
                <span className="text-muted-foreground">{t.projectDetail.sessionsCount}</span>
                <span className="font-medium tabular-nums">
                  {projSummary?.session_count ?? '–'} {t.projectDetail.teamsUnit}
                </span>
              </div>
              <div className="flex items-baseline gap-2">
                <span className="text-muted-foreground">{t.projectDetail.createdAt}</span>
                <span className="font-medium tabular-nums">{new Date(project.created_at).toLocaleDateString('zh-CN')}</span>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Tabs: 团队总览 / Ecosystem 设置 */}
      <Tabs defaultValue="teams">
        <TabsList variant="line" className="gap-3">
          <TabsTrigger value="teams">团队总览</TabsTrigger>
          <TabsTrigger value="ecosystem">Ecosystem 设置</TabsTrigger>
        </TabsList>

        <TabsContent value="teams" className="mt-4 space-y-6">
          {/* Leader Status */}
          <LeaderCard leaders={projSummary?.leaders ?? (projSummary?.leader ? [projSummary.leader] : null)} />

          {/* Worktrees */}
          <WorktreeCard worktrees={projSummary?.worktrees} />

          {/* Active Teams */}
          {activeTeams.length > 0 ? (
            <div className="space-y-4">
              {activeTeams.map((team) => {
                const wid = teamWfId(team);
                return (
                  <ActiveTeamContent
                    key={team.id}
                    team={team}
                    run={wid ? runByWfId[wid] : undefined}
                  />
                );
              })}
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
                  <CompletedTeamRow
                    key={team.id}
                    team={team}
                    run={(() => {
                      const wid = teamWfId(team);
                      return wid ? runByWfId[wid] : undefined;
                    })()}
                  />
                ))}
              </div>
            </div>
          )}
        </TabsContent>

        <TabsContent value="ecosystem" className="mt-4">
          {projectId && <EcosystemSettingsPanel projectId={projectId} />}
        </TabsContent>
      </Tabs>
    </div>
  );
}
