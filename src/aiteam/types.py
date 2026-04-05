"""AI Team OS — Global shared type definitions.

All modules reference types from this file; they do not define their own data models.
This file is managed by the tech-lead; other engineers only read-reference it.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

# ============================================================
# Enum types
# ============================================================


class OrchestrationMode(enum.StrEnum):
    """Team orchestration mode."""

    COORDINATE = "coordinate"
    BROADCAST = "broadcast"
    ROUTE = "route"
    MEET = "meet"


class TaskStatus(enum.StrEnum):
    """Task status."""

    PENDING = "pending"
    BLOCKED = "blocked"  # Has unfinished dependencies
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentStatus(enum.StrEnum):
    """Agent status — three-state model."""

    BUSY = "busy"  # Working — actively executing tool calls
    WAITING = "waiting"  # Waiting — alive but awaiting input (between turns)
    OFFLINE = "offline"  # Offline — terminated


class MeetingStatus(enum.StrEnum):
    """Meeting status."""

    ACTIVE = "active"
    CONCLUDED = "concluded"


class PhaseStatus(enum.StrEnum):
    """Phase status."""

    PLANNING = "planning"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class TeamStatus(enum.StrEnum):
    """Team lifecycle status."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class MeetingTemplate(enum.StrEnum):
    """Meeting template type."""

    BRAINSTORM = "brainstorm"  # Brainstorming (4 rounds)
    DECISION = "decision"  # Decision meeting (3 rounds)
    REVIEW = "review"  # Review meeting (3 rounds)
    RETROSPECTIVE = "retrospective"  # Retrospective meeting (3 rounds)
    STANDUP = "standup"  # Standup (1 round)
    DEBATE = "debate"  # Debate mode
    LEAN_COFFEE = "lean_coffee"  # Lean Coffee
    FREE = "free"  # Free discussion (default)


class LoopPhase(enum.StrEnum):
    """Company loop phase."""

    IDLE = "idle"
    PLANNING = "planning"
    ASSIGNING = "assigning"
    EXECUTING = "executing"
    MONITORING = "monitoring"
    REVIEWING = "reviewing"
    PAUSED = "paused"


class TaskPriority(enum.StrEnum):
    """Task priority."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskHorizon(enum.StrEnum):
    """Task time horizon."""

    SHORT = "short"
    MID = "mid"
    LONG = "long"


class MemoryScope(enum.StrEnum):
    """Memory scope."""

    GLOBAL = "global"
    TEAM = "team"
    AGENT = "agent"
    USER = "user"


class EventType(enum.StrEnum):
    """System event type."""

    # Team events
    TEAM_CREATED = "team.created"
    TEAM_DELETED = "team.deleted"
    TEAM_MODE_CHANGED = "team.mode_changed"

    # Agent events
    AGENT_CREATED = "agent.created"
    AGENT_REMOVED = "agent.removed"
    AGENT_STATUS_CHANGED = "agent.status_changed"

    # Task events
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"

    # Memory events
    MEMORY_CREATED = "memory.created"
    MEMORY_UPDATED = "memory.updated"
    MEMORY_ACCESSED = "memory.accessed"

    # Meeting events
    MEETING_STARTED = "meeting.started"
    MEETING_MESSAGE = "meeting.message"
    MEETING_ROUND_COMPLETED = "meeting.round_completed"
    MEETING_CONCLUDED = "meeting.concluded"

    # Hook/CC events
    AGENT_AUTO_REGISTERED = "agent.auto_registered"
    CC_TOOL_USE = "cc.tool_use"
    CC_TOOL_COMPLETE = "cc.tool_complete"
    CC_SESSION_START = "cc.session_start"
    CC_SESSION_END = "cc.session_end"

    # File events
    FILE_EDIT_CONFLICT = "file.edit_conflict"

    # Task lifecycle events
    TASK_STATUS_CHANGED = "task.status_changed"
    TASK_ASSIGNED = "task.assigned"

    # Task dependency events
    TASK_DECOMPOSED = "task.decomposed"
    TASK_BLOCKED = "task.blocked"
    TASK_UNBLOCKED = "task.unblocked"

    # System events
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPED = "system.stopped"
    SYSTEM_ERROR = "system.error"

    # Decision events (TOP2 cockpit — unified decision event stream)
    DECISION_TASK_ASSIGNED = "decision.task_assigned"
    DECISION_APPROACH_CHOSEN = "decision.approach_chosen"
    DECISION_AGENT_SELECTED = "decision.agent_selected"
    DECISION_AGENT_CREATED = "decision.agent_created"
    DECISION_MEETING_STARTED = "decision.meeting_started"

    # Knowledge events
    KNOWLEDGE_LESSON_LEARNED = "knowledge.lesson_learned"

    # Intent events
    INTENT_AGENT_WORKING = "intent.agent_working"

    # Enhanced event log (v0.9) — generic update events with state snapshots
    TASK_UPDATED = "task.updated"
    AGENT_UPDATED = "agent.updated"

    # Channel events (v1.0 P1-6)
    CHANNEL_MESSAGE = "channel.message"


# ============================================================
# Data models
# ============================================================


def _new_id() -> str:
    return str(uuid4())


class Project(BaseModel):
    """Project data model."""

    id: str = Field(default_factory=_new_id)
    name: str
    root_path: str = ""
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class Phase(BaseModel):
    """Phase data model — execution phase under a Project."""

    id: str = Field(default_factory=_new_id)
    project_id: str
    name: str
    description: str = ""
    status: PhaseStatus = PhaseStatus.PLANNING
    order: int = 0
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class Team(BaseModel):
    """Team data model."""

    id: str = Field(default_factory=_new_id)
    name: str
    mode: OrchestrationMode = OrchestrationMode.COORDINATE
    project_id: str | None = None
    leader_agent_id: str | None = None  # Leader agent for this team
    status: TeamStatus = TeamStatus.ACTIVE
    summary: str = ""  # One-line summary after team completion
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None


class Agent(BaseModel):
    """Agent data model."""

    id: str = Field(default_factory=_new_id)
    team_id: str
    name: str
    role: str
    system_prompt: str = ""
    model: str = "claude-opus-4-6"
    status: AgentStatus = AgentStatus.WAITING
    config: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"  # "api" = registered via CLAUDE.md, "hook" = auto-captured by hooks
    session_id: str | None = None  # Associated CC session ID
    cc_tool_use_id: str | None = None  # Associated CC internal agent ID
    current_task: str | None = None  # Currently executing task/activity description
    project_id: str | None = None
    current_phase_id: str | None = None
    trust_score: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=datetime.now)
    last_active_at: datetime | None = None


class Task(BaseModel):
    """Task data model."""

    id: str = Field(default_factory=_new_id)
    team_id: str | None = None
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    assigned_to: str | None = None
    result: str | None = None
    parent_id: str | None = None
    project_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    depth: int = 0
    order: int = 0
    template_id: str | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    horizon: TaskHorizon = TaskHorizon.SHORT
    tags: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class LoopState(BaseModel):
    """Company loop state — one per team."""

    team_id: str
    phase: LoopPhase = LoopPhase.IDLE
    prev_phase: LoopPhase | None = None
    current_cycle: int = 0
    completed_tasks_count: int = 0
    current_task_id: str | None = None
    review_interval: int = 5  # Trigger review every N tasks


class Memory(BaseModel):
    """Memory data model."""

    id: str = Field(default_factory=_new_id)
    scope: MemoryScope
    scope_id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    accessed_at: datetime = Field(default_factory=datetime.now)


class Event(BaseModel):
    """System event data model."""

    id: str = Field(default_factory=_new_id)
    type: EventType
    source: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)
    # Enhanced event context (v0.9)
    entity_id: str | None = None    # ID of the primary entity involved (task/agent/team)
    entity_type: str | None = None  # Entity type: "task" / "agent" / "team" / "meeting"
    state_snapshot: dict[str, Any] | None = None  # Trimmed key fields at event time


class Meeting(BaseModel):
    """Meeting data model."""

    id: str = Field(default_factory=_new_id)
    team_id: str
    topic: str
    status: MeetingStatus = MeetingStatus.ACTIVE
    participants: list[str] = Field(default_factory=list)
    project_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    concluded_at: datetime | None = None


class MeetingMessage(BaseModel):
    """Meeting message data model."""

    id: str = Field(default_factory=_new_id)
    meeting_id: str
    agent_id: str
    agent_name: str
    content: str
    round_number: int = 1
    timestamp: datetime = Field(default_factory=datetime.now)


class AgentActivity(BaseModel):
    """Agent activity record — logs each agent tool call."""

    id: str = Field(default_factory=_new_id)
    agent_id: str
    session_id: str
    tool_name: str  # Tool name (Bash, Edit, Read, Agent, etc.)
    input_summary: str = ""  # Input summary (e.g. command, file path)
    output_summary: str = ""  # Output summary (truncated to 500 chars)
    timestamp: datetime = Field(default_factory=datetime.now)
    duration_ms: int | None = None  # Tool call duration (ms), populated by Pre->Post correlation
    status: str = "completed"  # "running" | "completed" | "error"
    error: str | None = None  # Error message
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0


class CrossMessageType(enum.StrEnum):
    """Cross-project message type."""

    NOTIFICATION = "notification"
    REQUEST = "request"
    RESPONSE = "response"
    BROADCAST = "broadcast"


class CrossMessage(BaseModel):
    """Cross-project message — shared across all projects in the global DB."""

    id: str = Field(default_factory=_new_id)
    from_project_id: str
    from_project_dir: str
    to_project_id: str | None = None  # None means broadcast to all projects
    sender_name: str
    content: str
    message_type: CrossMessageType = CrossMessageType.NOTIFICATION
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    read_at: datetime | None = None


class ScheduledTask(BaseModel):
    """Scheduled task — periodic automation trigger."""

    id: str = Field(default_factory=_new_id)
    team_id: str | None = None
    name: str
    description: str = ""
    interval_seconds: int  # minimum 300 (5 min)
    action_type: str  # create_task / inject_reminder / emit_event
    action_config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime = Field(default_factory=datetime.now)
    created_at: datetime = Field(default_factory=datetime.now)


class WakeSession(BaseModel):
    """Record of a single wake_agent subprocess execution."""

    id: str = Field(default_factory=_new_id)
    scheduled_task_id: str
    agent_name: str
    team_id: str = ""
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: datetime | None = None
    outcome: str = ""  # completed / skipped_triage / timeout / error / fused / skipped_concurrent
    triage_result: str = ""
    stdout_summary: str = ""  # last 500 chars
    exit_code: int | None = None
    consecutive_failures: int = 0
    duration_seconds: float = 0.0


class LeaderBriefing(BaseModel):
    """Leader Briefing — pending decision items for user review."""

    id: str = Field(default_factory=_new_id)
    title: str
    description: str = ""
    options: str = ""  # A/B/C options description
    recommendation: str = ""  # Leader's suggested option
    urgency: str = "medium"  # high / medium / low
    status: str = "pending"  # pending / resolved / dismissed
    resolution: str = ""  # user's decision
    project_id: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    resolved_at: datetime | None = None


class ChannelMessage(BaseModel):
    """Channel message — supports cross-team broadcasting with @mention semantics."""

    id: str = Field(default_factory=_new_id)
    channel: str  # "team:<name>" / "project:<id>" / "global"
    sender: str
    content: str
    mentions: list[str] = Field(default_factory=list)  # ["@agent-name", "@team-name"]
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


# ============================================================
# Result types
# ============================================================


class TaskResult(BaseModel):
    """Task execution result."""

    task_id: str
    status: TaskStatus
    result: str
    agent_outputs: dict[str, str] = Field(default_factory=dict)
    duration_seconds: float = 0.0


class TeamStatusSummary(BaseModel):
    """Team status summary."""

    team: Team
    agents: list[Agent]
    active_tasks: list[Task]
    completed_tasks: int = 0
    total_tasks: int = 0


# ============================================================
# LangGraph state types
# ============================================================


class TeamState(dict):
    """LangGraph StateGraph state definition.

    Uses TypedDict style but inherits from dict for LangGraph compatibility.
    """

    pass


# TeamState field definitions (used for StateGraph channels)
TEAM_STATE_CHANNELS = {
    "team_id": str,
    "current_task": str,
    "messages": Annotated[list[BaseMessage], add_messages],
    "agent_outputs": dict[str, str],
    "leader_plan": str | None,
    "consensus_reached": bool,
    "round_number": int,
    "final_result": str | None,
    "approval_status": str | None,
}
