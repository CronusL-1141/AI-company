"""AI Team OS — SQLAlchemy ORM model definitions.

Maps Pydantic models from types.py to SQLAlchemy 2.0 ORM models
for SQLite data persistence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aiteam.types import (
    Agent,
    AgentActivity,
    AgentStatus,
    ChannelMessage,
    CrossMessage,
    CrossMessageType,
    Event,
    EventType,
    LeaderBriefing,
    Meeting,
    MeetingMessage,
    MeetingStatus,
    Memory,
    MemoryScope,
    OrchestrationMode,
    Phase,
    PhaseStatus,
    Project,
    Report,
    ScheduledTask,
    Task,
    TaskHorizon,
    TaskPriority,
    TaskStatus,
    Team,
    WakeSession,
)

# ============================================================
# Base class
# ============================================================


class Base(DeclarativeBase):
    """SQLAlchemy declarative base class."""

    pass


# ============================================================
# ORM Models
# ============================================================


class ProjectModel(Base):
    """Projects table."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    root_path: Mapped[str] = mapped_column(String(500), unique=True, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> Project:
        """Convert to Pydantic model."""
        return Project(
            id=self.id,
            name=self.name,
            root_path=self.root_path or "",
            description=self.description or "",
            config=self.config or {},
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @staticmethod
    def from_pydantic(project: Project) -> ProjectModel:
        """Create an ORM instance from a Pydantic model."""
        return ProjectModel(
            id=project.id,
            name=project.name,
            root_path=project.root_path,
            description=project.description,
            config=project.config,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )


class PhaseModel(Base):
    """Phases table."""

    __tablename__ = "phases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="planning")
    order: Mapped[int] = mapped_column(Integer, default=0)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> Phase:
        """Convert to Pydantic model."""
        return Phase(
            id=self.id,
            project_id=self.project_id,
            name=self.name,
            description=self.description or "",
            status=PhaseStatus(self.status),
            order=self.order or 0,
            config=self.config or {},
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @staticmethod
    def from_pydantic(phase: Phase) -> PhaseModel:
        """Create an ORM instance from a Pydantic model."""
        return PhaseModel(
            id=phase.id,
            project_id=phase.project_id,
            name=phase.name,
            description=phase.description,
            status=phase.status.value,
            order=phase.order,
            config=phase.config,
            created_at=phase.created_at,
            updated_at=phase.updated_at,
        )


class TeamModel(Base):
    """Teams table."""

    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="coordinate")
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    leader_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    summary: Mapped[str] = mapped_column(String(500), default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_pydantic(self) -> Team:
        """Convert to Pydantic model."""
        from aiteam.types import TeamStatus

        return Team(
            id=self.id,
            name=self.name,
            mode=OrchestrationMode(self.mode),
            project_id=self.project_id,
            leader_agent_id=self.leader_agent_id,
            status=TeamStatus(self.status) if self.status else TeamStatus.ACTIVE,
            summary=self.summary or "",
            config=self.config or {},
            created_at=self.created_at,
            updated_at=self.updated_at,
            completed_at=self.completed_at,
        )

    @staticmethod
    def from_pydantic(team: Team) -> TeamModel:
        """Create an ORM instance from a Pydantic model."""
        return TeamModel(
            id=team.id,
            name=team.name,
            mode=team.mode.value,
            project_id=team.project_id,
            leader_agent_id=team.leader_agent_id,
            status=team.status.value if hasattr(team.status, "value") else str(team.status),
            summary=team.summary,
            config=team.config,
            created_at=team.created_at,
            updated_at=team.updated_at,
            completed_at=team.completed_at,
        )


class AgentModel(Base):
    """Agents table."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(100), default="claude-opus-4-6")
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(20), default="api")
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cc_tool_use_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    current_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_phase_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trust_score: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_pydantic(self) -> Agent:
        """Convert to Pydantic model."""
        return Agent(
            id=self.id,
            team_id=self.team_id,
            name=self.name,
            role=self.role,
            system_prompt=self.system_prompt or "",
            model=self.model or "claude-opus-4-6",
            status=AgentStatus(self.status),
            config=self.config or {},
            source=self.source or "api",
            session_id=self.session_id,
            cc_tool_use_id=self.cc_tool_use_id,
            current_task=self.current_task,
            project_id=self.project_id,
            current_phase_id=self.current_phase_id,
            trust_score=self.trust_score if self.trust_score is not None else 0.5,
            created_at=self.created_at,
            last_active_at=self.last_active_at,
        )

    @staticmethod
    def from_pydantic(agent: Agent) -> AgentModel:
        """Create an ORM instance from a Pydantic model."""
        return AgentModel(
            id=agent.id,
            team_id=agent.team_id,
            name=agent.name,
            role=agent.role,
            system_prompt=agent.system_prompt,
            model=agent.model,
            status=agent.status.value,
            config=agent.config,
            source=agent.source,
            session_id=agent.session_id,
            cc_tool_use_id=agent.cc_tool_use_id,
            current_task=agent.current_task,
            project_id=agent.project_id,
            current_phase_id=agent.current_phase_id,
            trust_score=agent.trust_score,
            created_at=agent.created_at,
            last_active_at=agent.last_active_at,
        )


class TaskModel(Base):
    """Tasks table."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    assigned_to: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    depends_on: Mapped[dict[str, Any]] = mapped_column(JSON, default=list)
    depth: Mapped[int] = mapped_column(default=0)
    order: Mapped[int] = mapped_column(default=0)
    template_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    horizon: Mapped[str] = mapped_column(String(20), default="short")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_pydantic(self) -> Task:
        """Convert to Pydantic model."""
        return Task(
            id=self.id,
            team_id=self.team_id,
            title=self.title,
            description=self.description or "",
            status=TaskStatus(self.status),
            assigned_to=self.assigned_to,
            result=self.result,
            parent_id=self.parent_id,
            project_id=self.project_id,
            depends_on=self.depends_on if isinstance(self.depends_on, list) else [],
            depth=self.depth or 0,
            order=self.order or 0,
            template_id=self.template_id,
            priority=TaskPriority(self.priority) if self.priority else TaskPriority.MEDIUM,
            horizon=TaskHorizon(self.horizon) if self.horizon else TaskHorizon.SHORT,
            tags=self.tags if isinstance(self.tags, list) else [],
            config=self.config if isinstance(self.config, dict) else {},
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
        )

    @staticmethod
    def from_pydantic(task: Task) -> TaskModel:
        """Create an ORM instance from a Pydantic model."""
        return TaskModel(
            id=task.id,
            team_id=task.team_id,
            title=task.title,
            description=task.description,
            status=task.status.value,
            assigned_to=task.assigned_to,
            result=task.result,
            parent_id=task.parent_id,
            project_id=task.project_id,
            depends_on=task.depends_on,
            depth=task.depth,
            order=task.order,
            template_id=task.template_id,
            priority=task.priority.value,
            horizon=task.horizon.value,
            tags=task.tags,
            config=task.config,
            created_at=task.created_at,
            started_at=task.started_at,
            completed_at=task.completed_at,
        )


class MemoryModel(Base):
    """Memories table."""

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    accessed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> Memory:
        """Convert to Pydantic model."""
        return Memory(
            id=self.id,
            scope=MemoryScope(self.scope),
            scope_id=self.scope_id,
            content=self.content,
            metadata=self.metadata_json or {},
            created_at=self.created_at,
            accessed_at=self.accessed_at,
        )

    @staticmethod
    def from_pydantic(memory: Memory) -> MemoryModel:
        """Create an ORM instance from a Pydantic model."""
        return MemoryModel(
            id=memory.id,
            scope=memory.scope.value,
            scope_id=memory.scope_id,
            content=memory.content,
            metadata_json=memory.metadata,
            created_at=memory.created_at,
            accessed_at=memory.accessed_at,
        )


class EventModel(Base):
    """Events table."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # Enhanced event context fields (v0.9)
    entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    state_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    def to_pydantic(self) -> Event:
        """Convert to Pydantic model."""
        return Event(
            id=self.id,
            type=EventType(self.type),
            source=self.source,
            data=self.data or {},
            timestamp=self.timestamp,
            entity_id=self.entity_id,
            entity_type=self.entity_type,
            state_snapshot=self.state_snapshot,
        )

    @staticmethod
    def from_pydantic(event: Event) -> EventModel:
        """Create an ORM instance from a Pydantic model."""
        return EventModel(
            id=event.id,
            type=event.type.value,
            source=event.source,
            data=event.data,
            timestamp=event.timestamp,
            entity_id=event.entity_id,
            entity_type=event.entity_type,
            state_snapshot=event.state_snapshot,
        )


class MeetingModel(Base):
    """Meetings table."""

    __tablename__ = "meetings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")
    participants: Mapped[list[str]] = mapped_column(JSON, default=list)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    concluded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_pydantic(self) -> Meeting:
        """Convert to Pydantic model."""
        return Meeting(
            id=self.id,
            team_id=self.team_id,
            topic=self.topic,
            status=MeetingStatus(self.status),
            participants=self.participants or [],
            project_id=self.project_id,
            created_at=self.created_at,
            concluded_at=self.concluded_at,
        )

    @staticmethod
    def from_pydantic(meeting: Meeting) -> MeetingModel:
        """Create an ORM instance from a Pydantic model."""
        return MeetingModel(
            id=meeting.id,
            team_id=meeting.team_id,
            topic=meeting.topic,
            status=meeting.status.value,
            participants=meeting.participants,
            project_id=meeting.project_id,
            created_at=meeting.created_at,
            concluded_at=meeting.concluded_at,
        )


class MeetingMessageModel(Base):
    """Meeting messages table."""

    __tablename__ = "meeting_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    round_number: Mapped[int] = mapped_column(default=1)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> MeetingMessage:
        """Convert to Pydantic model."""
        return MeetingMessage(
            id=self.id,
            meeting_id=self.meeting_id,
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            content=self.content,
            round_number=self.round_number,
            timestamp=self.timestamp,
        )

    @staticmethod
    def from_pydantic(msg: MeetingMessage) -> MeetingMessageModel:
        """Create an ORM instance from a Pydantic model."""
        return MeetingMessageModel(
            id=msg.id,
            meeting_id=msg.meeting_id,
            agent_id=msg.agent_id,
            agent_name=msg.agent_name,
            content=msg.content,
            round_number=msg.round_number,
            timestamp=msg.timestamp,
        )


class AgentActivityModel(Base):
    """Agent activity records table."""

    __tablename__ = "agent_activities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="completed")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    def to_pydantic(self) -> AgentActivity:
        """Convert to Pydantic model."""
        return AgentActivity(
            id=self.id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            tool_name=self.tool_name,
            input_summary=self.input_summary or "",
            output_summary=self.output_summary or "",
            timestamp=self.timestamp,
            duration_ms=self.duration_ms,
            status=self.status or "completed",
            error=self.error,
            tokens_input=self.tokens_input or 0,
            tokens_output=self.tokens_output or 0,
            cost_usd=self.cost_usd or 0.0,
        )

    @staticmethod
    def from_pydantic(activity: AgentActivity) -> AgentActivityModel:
        """Create an ORM instance from a Pydantic model."""
        return AgentActivityModel(
            id=activity.id,
            agent_id=activity.agent_id,
            session_id=activity.session_id,
            tool_name=activity.tool_name,
            input_summary=activity.input_summary,
            output_summary=activity.output_summary,
            timestamp=activity.timestamp,
            duration_ms=activity.duration_ms,
            status=activity.status,
            error=activity.error,
            tokens_input=activity.tokens_input,
            tokens_output=activity.tokens_output,
            cost_usd=activity.cost_usd,
        )


class ScheduledTaskModel(Base):
    """Scheduled tasks table."""

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> ScheduledTask:
        """Convert to Pydantic model."""
        return ScheduledTask(
            id=self.id,
            team_id=self.team_id,
            name=self.name,
            description=self.description or "",
            interval_seconds=self.interval_seconds,
            action_type=self.action_type,
            action_config=self.action_config or {},
            enabled=self.enabled,
            last_run_at=self.last_run_at,
            next_run_at=self.next_run_at,
            created_at=self.created_at,
        )

    @staticmethod
    def from_pydantic(task: ScheduledTask) -> ScheduledTaskModel:
        """Create an ORM instance from a Pydantic model."""
        return ScheduledTaskModel(
            id=task.id,
            team_id=task.team_id,
            name=task.name,
            description=task.description,
            interval_seconds=task.interval_seconds,
            action_type=task.action_type,
            action_config=task.action_config,
            enabled=task.enabled,
            last_run_at=task.last_run_at,
            next_run_at=task.next_run_at,
            created_at=task.created_at,
        )


class CrossMessageModel(Base):
    """Cross-project messages table — stored in the global default DB."""

    __tablename__ = "cross_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    from_project_id: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    from_project_dir: Mapped[str] = mapped_column(String(500), nullable=False)
    to_project_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    sender_name: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(String(20), nullable=False, default="notification")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_pydantic(self) -> CrossMessage:
        """Convert to Pydantic model."""
        return CrossMessage(
            id=self.id,
            from_project_id=self.from_project_id,
            from_project_dir=self.from_project_dir,
            to_project_id=self.to_project_id,
            sender_name=self.sender_name,
            content=self.content,
            message_type=CrossMessageType(self.message_type),
            metadata=self.metadata_json or {},
            created_at=self.created_at,
            read_at=self.read_at,
        )

    @staticmethod
    def from_pydantic(msg: CrossMessage) -> CrossMessageModel:
        """Create an ORM instance from a Pydantic model."""
        return CrossMessageModel(
            id=msg.id,
            from_project_id=msg.from_project_id,
            from_project_dir=msg.from_project_dir,
            to_project_id=msg.to_project_id,
            sender_name=msg.sender_name,
            content=msg.content,
            message_type=msg.message_type.value,
            metadata_json=msg.metadata,
            created_at=msg.created_at,
            read_at=msg.read_at,
        )


class WakeSessionModel(Base):
    """Wake sessions table — records each wake_agent subprocess execution."""

    __tablename__ = "wake_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scheduled_task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(String(36), nullable=True, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    triage_result: Mapped[str] = mapped_column(Text, nullable=True, default="")
    stdout_summary: Mapped[str] = mapped_column(Text, nullable=True, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    def to_pydantic(self) -> WakeSession:
        """Convert to Pydantic model."""
        return WakeSession(
            id=self.id,
            scheduled_task_id=self.scheduled_task_id,
            agent_name=self.agent_name,
            team_id=self.team_id or "",
            started_at=self.started_at,
            finished_at=self.finished_at,
            outcome=self.outcome or "",
            triage_result=self.triage_result or "",
            stdout_summary=self.stdout_summary or "",
            exit_code=self.exit_code,
            consecutive_failures=self.consecutive_failures,
            duration_seconds=self.duration_seconds,
        )

    @staticmethod
    def from_pydantic(ws: WakeSession) -> WakeSessionModel:
        """Create an ORM instance from a Pydantic model."""
        return WakeSessionModel(
            id=ws.id,
            scheduled_task_id=ws.scheduled_task_id,
            agent_name=ws.agent_name,
            team_id=ws.team_id,
            started_at=ws.started_at,
            finished_at=ws.finished_at,
            outcome=ws.outcome,
            triage_result=ws.triage_result,
            stdout_summary=ws.stdout_summary,
            exit_code=ws.exit_code,
            consecutive_failures=ws.consecutive_failures,
            duration_seconds=ws.duration_seconds,
        )


class LeaderBriefingModel(Base):
    """Leader briefings table — pending decision items for user review."""

    __tablename__ = "leader_briefings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")
    options: Mapped[str] = mapped_column(Text, nullable=True, default="")
    recommendation: Mapped[str] = mapped_column(Text, nullable=True, default="")
    urgency: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    resolution: Mapped[str] = mapped_column(Text, nullable=True, default="")
    project_id: Mapped[str] = mapped_column(String(36), nullable=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_pydantic(self) -> LeaderBriefing:
        """Convert to Pydantic model."""
        return LeaderBriefing(
            id=self.id,
            title=self.title,
            description=self.description or "",
            options=self.options or "",
            recommendation=self.recommendation or "",
            urgency=self.urgency or "medium",
            status=self.status or "pending",
            resolution=self.resolution or "",
            project_id=self.project_id or "",
            created_at=self.created_at,
            resolved_at=self.resolved_at,
        )

    @staticmethod
    def from_pydantic(briefing: LeaderBriefing) -> LeaderBriefingModel:
        """Create an ORM instance from a Pydantic model."""
        return LeaderBriefingModel(
            id=briefing.id,
            title=briefing.title,
            description=briefing.description,
            options=briefing.options,
            recommendation=briefing.recommendation,
            urgency=briefing.urgency,
            status=briefing.status,
            resolution=briefing.resolution,
            project_id=briefing.project_id,
            created_at=briefing.created_at,
            resolved_at=briefing.resolved_at,
        )


class ChannelMessageModel(Base):
    """Channel messages table — stores cross-team messages with @mention semantics."""

    __tablename__ = "channel_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    channel: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    sender: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mentions: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> ChannelMessage:
        """Convert to Pydantic model."""
        return ChannelMessage(
            id=self.id,
            channel=self.channel,
            sender=self.sender,
            content=self.content,
            mentions=self.mentions if isinstance(self.mentions, list) else [],
            metadata=self.metadata_json or {},
            created_at=self.created_at,
        )

    @staticmethod
    def from_pydantic(msg: ChannelMessage) -> ChannelMessageModel:
        """Create an ORM instance from a Pydantic model."""
        return ChannelMessageModel(
            id=msg.id,
            channel=msg.channel,
            sender=msg.sender,
            content=msg.content,
            mentions=msg.mentions,
            metadata_json=msg.metadata,
            created_at=msg.created_at,
        )


class ReportModel(Base):
    """Reports table — research/analysis reports with project isolation."""

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=True, default="", index=True)
    author: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    topic: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    report_type: Mapped[str] = mapped_column(String(50), nullable=False, default="research")
    date_str: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    task_id: Mapped[str] = mapped_column(String(36), nullable=True, default="")
    team_id: Mapped[str] = mapped_column(String(36), nullable=True, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> Report:
        """Convert to Pydantic model."""
        return Report(
            id=self.id,
            project_id=self.project_id or "",
            author=self.author or "",
            topic=self.topic or "",
            report_type=self.report_type or "research",
            date=self.date_str or "",
            content=self.content or "",
            task_id=self.task_id or "",
            team_id=self.team_id or "",
            created_at=self.created_at,
        )

    @staticmethod
    def from_pydantic(report: Report) -> ReportModel:
        """Create an ORM instance from a Pydantic model."""
        return ReportModel(
            id=report.id,
            project_id=report.project_id,
            author=report.author,
            topic=report.topic,
            report_type=report.report_type,
            date_str=report.date,
            content=report.content,
            task_id=report.task_id,
            team_id=report.team_id,
            created_at=report.created_at,
        )
