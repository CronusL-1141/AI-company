"""AI Team OS — SQLAlchemy ORM model definitions.

Maps Pydantic models from types.py to SQLAlchemy 2.0 ORM models
for SQLite data persistence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aiteam.types import (
    Agent,
    AgentActivity,
    AgentStatus,
    ChannelMessage,
    CrossMessage,
    CrossMessageType,
    DataSource,
    DataSourceKind,
    DemoResult,
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemIndexDiff,
    EcosystemProjectSettings,
    EcosystemRelation,
    EcosystemRelationType,
    EcosystemRepoEvent,
    EcosystemRepoProfile,
    EcosystemRepoStatusSnapshot,
    EcosystemRepoTag,
    EcosystemScanRun,
    EcosystemScanStrategy,
    EcosystemShallowBatch,
    EcosystemStageStatus,
    EcosystemStatusChange,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
    Event,
    EventType,
    IntegrationRecommendation,
    KnowledgeLink,
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
    ScanProfile,
    ScheduledTask,
    StageTransition,
    Task,
    TaskHorizon,
    TaskMemo,
    TaskPriority,
    TaskStatus,
    Team,
    WakeSession,
    WorkflowAgent,
    WorkflowRun,
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
    __table_args__ = (
        # Partial UNIQUE on cc_tool_use_id: two concurrent create-if-absent paths
        # (SubagentStart coroutine vs reaper live-tail member reap) must not both
        # insert a member row for the same CC tool_use id → duplicate member rows /
        # orphan busy (audit B1). NULL *and empty-string* cc ids (leaders, plain api
        # agents, or subagents whose agent_id was trimmed from an oversized payload)
        # mean "no cc id" and are exempt — otherwise two distinct cc-less members would
        # collapse onto one '' key. Existing file DBs get this via
        # connection._ensure_agents_cc_tool_use_id_unique (dedup + CREATE UNIQUE INDEX);
        # new/in-memory DBs get it here through create_all.
        Index(
            "uq_agents_cc_tool_use_id",
            "cc_tool_use_id",
            unique=True,
            sqlite_where=text("cc_tool_use_id IS NOT NULL AND cc_tool_use_id != ''"),
            postgresql_where=text("cc_tool_use_id IS NOT NULL AND cc_tool_use_id != ''"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(100), default="")
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
    # Agent reuse governance P1 (batch 1B): sub-agent context watermark ledger.
    # See docs/agent-reuse-design.md section 4.3. reuse_domain is provisioned for
    # the P2 decision layer and stays NULL in P1.
    ctx_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ctx_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ctx_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    transcript_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    ctx_measured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reuse_domain: Mapped[str | None] = mapped_column(String(200), nullable=True)

    def to_pydantic(self) -> Agent:
        """Convert to Pydantic model."""
        return Agent(
            id=self.id,
            team_id=self.team_id,
            name=self.name,
            role=self.role,
            system_prompt=self.system_prompt or "",
            # 读路径绝不注入具体型号：DB 为空就如实返回空（曾在此把清洗后的
            # 空值又"读成"claude-opus-4-7，DB 干净而 API 说谎——2026-07-07 实录）。
            model=self.model or "",
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
            ctx_tokens=self.ctx_tokens,
            ctx_window=self.ctx_window,
            ctx_pct=self.ctx_pct,
            transcript_path=self.transcript_path,
            ctx_measured_at=self.ctx_measured_at,
            reuse_domain=self.reuse_domain,
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
            ctx_tokens=agent.ctx_tokens,
            ctx_window=agent.ctx_window,
            ctx_pct=agent.ctx_pct,
            transcript_path=agent.transcript_path,
            ctx_measured_at=agent.ctx_measured_at,
            reuse_domain=agent.reuse_domain,
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


class TaskMemoModel(Base):
    """Task memos table (记忆系统 v2 P0：情景层升表).

    从 tasks.config['memo'] JSON 数组升为独立行，带行级 ID + 失效轴 + 质量分。
    表随 Base.metadata.create_all 自动建（含下方两个索引），无需列迁移。
    """

    __tablename__ = "task_memos"
    __table_args__ = (
        Index("idx_memos_task", "task_id"),
        Index("idx_memos_valid", "project_id", "invalid_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    author: Mapped[str] = mapped_column(String(100), default="leader")
    memo_type: Mapped[str] = mapped_column(String(20), default="progress")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    scope_path: Mapped[str] = mapped_column(String(200), default="")
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invalidated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )

    def to_pydantic(self) -> TaskMemo:
        """Convert to Pydantic model."""
        return TaskMemo(
            id=self.id,
            task_id=self.task_id,
            project_id=self.project_id,
            author=self.author or "leader",
            memo_type=self.memo_type or "progress",
            content=self.content,
            scope_path=self.scope_path or "",
            quality_score=self.quality_score,
            invalid_at=self.invalid_at,
            invalidated_by=self.invalidated_by,
            meta=self.meta if isinstance(self.meta, dict) else {},
            created_at=self.created_at,
        )

    @staticmethod
    def from_pydantic(memo: TaskMemo) -> TaskMemoModel:
        """Create an ORM instance from a Pydantic model."""
        return TaskMemoModel(
            id=memo.id,
            task_id=memo.task_id,
            project_id=memo.project_id,
            author=memo.author,
            memo_type=memo.memo_type,
            content=memo.content,
            scope_path=memo.scope_path,
            quality_score=memo.quality_score,
            invalid_at=memo.invalid_at,
            invalidated_by=memo.invalidated_by,
            meta=memo.meta,
            created_at=memo.created_at,
        )


class MemoryModel(Base):
    """Memories table.

    记忆系统 v2 P1：方向层激活——加 kind/invalid_at/invalidated_by/source_refs 四列
    （既有库经 connection.COLUMNS_TO_ENSURE 补列，新库/内存库由 create_all 直接建齐）。
    """

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="preference")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    source_refs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invalidated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    accessed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> Memory:
        """Convert to Pydantic model."""
        return Memory(
            id=self.id,
            scope=MemoryScope(self.scope),
            scope_id=self.scope_id,
            content=self.content,
            kind=self.kind or "preference",
            metadata=self.metadata_json or {},
            source_refs=self.source_refs if isinstance(self.source_refs, list) else [],
            invalid_at=self.invalid_at,
            invalidated_by=self.invalidated_by,
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
            kind=memory.kind,
            metadata_json=memory.metadata,
            source_refs=memory.source_refs,
            invalid_at=memory.invalid_at,
            invalidated_by=memory.invalidated_by,
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
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=True)
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
            meta_json=self.meta_json or {},
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
            meta_json=meeting.meta_json,
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
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

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
            msg_metadata=self.metadata_json or {},
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
            metadata_json=msg.msg_metadata or {},
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


class PipelineStageHistoryModel(Base):
    """Append-only stage transition log. Agent 不应有 UPDATE/DELETE 权限。"""

    __tablename__ = "pipeline_stage_history"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    from_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    to_stage: Mapped[str] = mapped_column(String, nullable=False)
    transitioned_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )
    triggered_by: Mapped[str] = mapped_column(String, default="manual")
    reason: Mapped[str] = mapped_column(Text, default="")

    def to_pydantic(self) -> StageTransition:
        """Convert to Pydantic model."""
        return StageTransition(
            id=self.id,
            task_id=self.task_id,
            from_stage=self.from_stage,
            to_stage=self.to_stage,
            transitioned_at=self.transitioned_at,
            triggered_by=self.triggered_by,  # type: ignore[arg-type]
            reason=self.reason or "",
        )

    @classmethod
    def from_pydantic(cls, p: StageTransition) -> PipelineStageHistoryModel:
        """Create an ORM instance from a Pydantic model."""
        return cls(
            id=p.id,
            task_id=p.task_id,
            from_stage=p.from_stage,
            to_stage=p.to_stage,
            transitioned_at=p.transitioned_at,
            triggered_by=p.triggered_by,
            reason=p.reason,
        )


class EcosystemRepoProfileModel(Base):
    """Claude 生态仓档案 — 支持广索引检索 + 周期更新。

    项目隔离：每个 project_id 拥有独立的仓快照（同一仓在不同项目下是不同行）。
    历史数据 project_id=NULL 视为"全局/未归属"，迁移时 backfill 至默认项目。
    """

    __tablename__ = "ecosystem_repo_profiles"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "repo_full_name",
            name="uq_ecosystem_profiles_project_repo",
        ),
        # K1 perf indexes — composite covers (project filter + sort) hot paths.
        Index(
            "ix_ecosystem_profiles_project_stars",
            "project_id",
            "stars",
        ),
        Index(
            "ix_ecosystem_profiles_project_category_stars",
            "project_id",
            "relevance_category",
            "stars",
        ),
        Index(
            "ix_ecosystem_profiles_project_lang_stars",
            "project_id",
            "language",
            "stars",
        ),
        Index(
            "ix_ecosystem_profiles_project_pushed",
            "project_id",
            "pushed_at",
        ),
        Index(
            "ix_ecosystem_profiles_project_archived_stars",
            "project_id",
            "is_archived",
            "stars",
        ),
        # v1.5.0-A perf — active set hot path (filter is_active + is_deleted/private)
        Index(
            "ix_ecosystem_profiles_project_active_stars",
            "project_id",
            "is_active",
            "stars",
        ),
        Index(
            "ix_ecosystem_profiles_project_deleted_private",
            "project_id",
            "is_deleted",
            "is_private_now",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    repo_full_name: Mapped[str] = mapped_column(String(200), index=True)
    name: Mapped[str] = mapped_column(String(200))
    owner: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    stars: Mapped[int] = mapped_column(Integer, index=True)
    language: Mapped[str | None] = mapped_column(String(50), nullable=True)
    topics: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-serialized list
    homepage: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_commit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    needs_deep_review: Mapped[bool] = mapped_column(Boolean, default=False)
    relevance_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    relevance_score: Mapped[int] = mapped_column(Integer, default=0)
    one_line_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scanned_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )
    # Stage B 扩展字段
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    scan_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    description_excerpt: Mapped[str] = mapped_column(Text, default="")
    # v1.5.0-A 扩展字段：浅扫 + 失败追踪 + 活跃集
    shallow_summary: Mapped[str] = mapped_column(Text, default="")
    last_shallow_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_private_now: Mapped[bool] = mapped_column(Boolean, default=False)
    last_fetch_error: Mapped[str] = mapped_column(Text, default="")
    fetch_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    active_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # v1.6.0-P0 fields (also in COLUMNS_TO_ENSURE for existing file-based DBs)
    canonical_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(20), default="github")
    last_active_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_status_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v1.6.0-P0.4 NormalizedSignal fields
    popularity_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    activity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # v1.6.0-P1.A manual status
    manual_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    manual_status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_status_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_status_set_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # v1.6.0-P1.C-1: JSON-serialized list of query strings
    discovered_via_queries: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v1.6.1 multi-source: JSON list [{kind,id,stars/likes,url,last_seen_at}, ...]
    sources: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v1.6.1 primary_source: decides canonical_id + UI primary link ("github"/"hf_space"/"gitlab")
    primary_source: Mapped[str] = mapped_column(String(20), default="github")

    def to_pydantic(self) -> EcosystemRepoProfile:
        """Convert to Pydantic model."""
        import json

        topics_list: list[str] = []
        if self.topics:
            try:
                topics_list = json.loads(self.topics)
            except Exception:
                topics_list = []

        discovered_via_queries_list: list[str] = []
        if self.discovered_via_queries:
            try:
                discovered_via_queries_list = json.loads(self.discovered_via_queries)
            except Exception:
                discovered_via_queries_list = []

        sources_list: list[dict] = []
        if self.sources:
            try:
                sources_list = json.loads(self.sources)
            except Exception:
                sources_list = []

        return EcosystemRepoProfile(
            id=self.id,
            project_id=self.project_id,
            repo_full_name=self.repo_full_name,
            name=self.name,
            owner=self.owner,
            description=self.description,
            stars=self.stars,
            language=self.language,
            topics=topics_list,
            homepage=self.homepage,
            last_commit_at=self.last_commit_at,
            needs_deep_review=self.needs_deep_review,
            relevance_category=self.relevance_category,
            relevance_score=self.relevance_score,
            one_line_summary=self.one_line_summary,
            last_scanned_at=self.last_scanned_at,
            first_seen_at=self.first_seen_at,
            pushed_at=self.pushed_at,
            is_archived=bool(self.is_archived) if self.is_archived is not None else False,
            scan_run_id=self.scan_run_id,
            description_excerpt=self.description_excerpt or "",
            shallow_summary=self.shallow_summary or "",
            last_shallow_refreshed_at=self.last_shallow_refreshed_at,
            is_deleted=bool(self.is_deleted) if self.is_deleted is not None else False,
            is_private_now=bool(self.is_private_now) if self.is_private_now is not None else False,
            last_fetch_error=self.last_fetch_error or "",
            fetch_failure_count=self.fetch_failure_count or 0,
            is_active=bool(self.is_active) if self.is_active is not None else True,
            active_rank=self.active_rank,
            canonical_id=self.canonical_id,
            source_kind=self.source_kind or "github",
            last_active_status=self.last_active_status,
            last_status_change_at=self.last_status_change_at,
            popularity_percentile=self.popularity_percentile,
            activity_score=self.activity_score,
            manual_status=self.manual_status,
            manual_status_reason=self.manual_status_reason,
            manual_status_set_at=self.manual_status_set_at,
            manual_status_set_by=self.manual_status_set_by,
            discovered_via_queries=discovered_via_queries_list,
            sources=sources_list,
            primary_source=self.primary_source or "github",
        )

    @classmethod
    def from_pydantic(cls, p: EcosystemRepoProfile) -> EcosystemRepoProfileModel:
        """Create an ORM instance from a Pydantic model."""
        import json

        return cls(
            id=p.id,
            project_id=p.project_id,
            repo_full_name=p.repo_full_name,
            name=p.name,
            owner=p.owner,
            description=p.description,
            stars=p.stars,
            language=p.language,
            topics=json.dumps(p.topics) if p.topics else None,
            homepage=p.homepage,
            last_commit_at=p.last_commit_at,
            needs_deep_review=p.needs_deep_review,
            relevance_category=p.relevance_category,
            relevance_score=p.relevance_score,
            one_line_summary=p.one_line_summary,
            last_scanned_at=p.last_scanned_at,
            first_seen_at=p.first_seen_at,
            pushed_at=p.pushed_at,
            is_archived=p.is_archived,
            scan_run_id=p.scan_run_id,
            description_excerpt=p.description_excerpt or "",
            shallow_summary=p.shallow_summary or "",
            last_shallow_refreshed_at=p.last_shallow_refreshed_at,
            is_deleted=p.is_deleted,
            is_private_now=p.is_private_now,
            last_fetch_error=p.last_fetch_error or "",
            fetch_failure_count=p.fetch_failure_count or 0,
            is_active=p.is_active,
            active_rank=p.active_rank,
            canonical_id=p.canonical_id,
            source_kind=p.source_kind or "github",
            last_active_status=p.last_active_status,
            last_status_change_at=p.last_status_change_at,
            popularity_percentile=p.popularity_percentile,
            activity_score=p.activity_score,
            manual_status=p.manual_status,
            manual_status_reason=p.manual_status_reason,
            manual_status_set_at=p.manual_status_set_at,
            manual_status_set_by=p.manual_status_set_by,
            discovered_via_queries=json.dumps(p.discovered_via_queries) if p.discovered_via_queries else None,
            sources=json.dumps(p.sources) if p.sources else None,
            primary_source=p.primary_source or "github",
        )


# ============================================================
# Ecosystem Stage B — 5 个扩展表
# ============================================================


class EcosystemDeepReviewModel(Base):
    """生态仓深扫报告 ORM 模型。

    项目隔离：每个 project_id 拥有独立的深扫报告。
    历史数据 project_id=NULL，迁移时 backfill 至默认项目。
    """

    __tablename__ = "ecosystem_deep_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    repo_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    summary_md: Mapped[str] = mapped_column(Text, default="")
    architecture_md: Mapped[str] = mapped_column(Text, default="")
    demo_result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    demo_log_excerpt: Mapped[str] = mapped_column(Text, default="")
    risks_md: Mapped[str] = mapped_column(Text, default="")
    learnings_md: Mapped[str] = mapped_column(Text, default="")
    integration_recommendation: Mapped[str | None] = mapped_column(String(20), nullable=True)
    report_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dispatch_prompt: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )
    # v1.5.0-A 扩展字段：渐进式漏斗 stage + 关联会议/集成任务
    stage_status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    integration_md: Mapped[str] = mapped_column(Text, default="")
    shallow_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    architecture_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    debated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stage3_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    debate_meeting_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    integration_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # v1.5.3: worker pool claim 字段
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # v1.7.0: 关联浅扫批次
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    def to_pydantic(self) -> EcosystemDeepReview:
        """Convert to Pydantic model."""
        return EcosystemDeepReview(
            id=self.id,
            project_id=self.project_id,
            repo_id=self.repo_id,
            status=EcosystemDeepReviewStatus(self.status),
            agent_id=self.agent_id,
            summary_md=self.summary_md or "",
            architecture_md=self.architecture_md or "",
            demo_result=DemoResult(self.demo_result) if self.demo_result else None,
            demo_log_excerpt=self.demo_log_excerpt or "",
            risks_md=self.risks_md or "",
            learnings_md=self.learnings_md or "",
            integration_recommendation=(
                IntegrationRecommendation(self.integration_recommendation)
                if self.integration_recommendation
                else None
            ),
            report_id=self.report_id,
            dispatch_prompt=self.dispatch_prompt or "",
            started_at=self.started_at,
            completed_at=self.completed_at,
            duration_seconds=self.duration_seconds or 0.0,
            created_at=self.created_at,
            stage_status=EcosystemStageStatus(self.stage_status) if self.stage_status else EcosystemStageStatus.QUEUED,
            integration_md=self.integration_md or "",
            shallow_completed_at=self.shallow_completed_at,
            architecture_completed_at=self.architecture_completed_at,
            debated_at=self.debated_at,
            stage3_completed_at=self.stage3_completed_at,
            debate_meeting_id=self.debate_meeting_id,
            integration_task_id=self.integration_task_id,
            claimed_by=self.claimed_by,
            claimed_at=self.claimed_at,
            quality_score=self.quality_score,
            quality_notes=self.quality_notes,
            reviewed_by=self.reviewed_by,
            reviewed_at=self.reviewed_at,
            batch_id=getattr(self, "batch_id", None),
        )

    @classmethod
    def from_pydantic(cls, p: EcosystemDeepReview) -> EcosystemDeepReviewModel:
        """Create an ORM instance from a Pydantic model."""
        return cls(
            id=p.id,
            project_id=p.project_id,
            repo_id=p.repo_id,
            status=p.status.value,
            agent_id=p.agent_id,
            summary_md=p.summary_md,
            architecture_md=p.architecture_md,
            demo_result=p.demo_result.value if p.demo_result else None,
            demo_log_excerpt=p.demo_log_excerpt,
            risks_md=p.risks_md,
            learnings_md=p.learnings_md,
            integration_recommendation=(
                p.integration_recommendation.value if p.integration_recommendation else None
            ),
            report_id=p.report_id,
            dispatch_prompt=p.dispatch_prompt,
            started_at=p.started_at,
            completed_at=p.completed_at,
            duration_seconds=p.duration_seconds,
            created_at=p.created_at,
            stage_status=p.stage_status.value if hasattr(p.stage_status, "value") else str(p.stage_status or "queued"),
            integration_md=p.integration_md or "",
            shallow_completed_at=p.shallow_completed_at,
            architecture_completed_at=p.architecture_completed_at,
            debated_at=p.debated_at,
            stage3_completed_at=p.stage3_completed_at,
            debate_meeting_id=p.debate_meeting_id,
            integration_task_id=p.integration_task_id,
            claimed_by=p.claimed_by,
            claimed_at=p.claimed_at,
            quality_score=p.quality_score,
            quality_notes=p.quality_notes,
            reviewed_by=p.reviewed_by,
            reviewed_at=p.reviewed_at,
            batch_id=p.batch_id,
        )


class EcosystemTagModel(Base):
    """能力标签字典 ORM 模型。"""

    __tablename__ = "ecosystem_tags"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )

    def to_pydantic(self) -> EcosystemTag:
        """Convert to Pydantic model."""
        return EcosystemTag(
            id=self.id,
            name=self.name,
            aliases=self.aliases if isinstance(self.aliases, list) else [],
            category=EcosystemTagCategory(self.category),
            description=self.description or "",
            created_at=self.created_at,
        )

    @classmethod
    def from_pydantic(cls, p: EcosystemTag) -> EcosystemTagModel:
        """Create an ORM instance from a Pydantic model."""
        return cls(
            id=p.id,
            name=p.name,
            aliases=p.aliases,
            category=p.category.value,
            description=p.description,
            created_at=p.created_at,
        )


class EcosystemRepoTagModel(Base):
    """仓-标签多对多关联 ORM 模型。

    项目隔离：每个 project_id 内部 (repo_id, tag_id) 唯一；不同项目可独立持有同一关联。
    实践中 repo 已经是 per-project，因此 (repo_id, tag_id) 全表唯一也成立。
    """

    __tablename__ = "ecosystem_repo_tags"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    repo_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tag_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )

    __table_args__ = (
        UniqueConstraint("repo_id", "tag_id", name="uq_ecosystem_repo_tags_repo_tag"),
    )

    def to_pydantic(self) -> EcosystemRepoTag:
        """Convert to Pydantic model."""
        return EcosystemRepoTag(
            id=self.id,
            project_id=self.project_id,
            repo_id=self.repo_id,
            tag_id=self.tag_id,
            confidence=self.confidence if self.confidence is not None else 1.0,
            source=EcosystemTagSource(self.source),
            agent_id=self.agent_id,
            created_at=self.created_at,
        )

    @classmethod
    def from_pydantic(cls, p: EcosystemRepoTag) -> EcosystemRepoTagModel:
        """Create an ORM instance from a Pydantic model."""
        return cls(
            id=p.id,
            project_id=p.project_id,
            repo_id=p.repo_id,
            tag_id=p.tag_id,
            confidence=p.confidence,
            source=p.source.value,
            agent_id=p.agent_id,
            created_at=p.created_at,
        )


class EcosystemRelationModel(Base):
    """仓与仓的关联关系 ORM 模型。

    项目隔离：仓与仓的关系是项目内部的研究产出，不跨项目共享。
    历史数据 project_id=NULL，迁移时 backfill 至默认项目。
    """

    __tablename__ = "ecosystem_relations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    from_repo_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    to_repo_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )

    def to_pydantic(self) -> EcosystemRelation:
        """Convert to Pydantic model."""
        return EcosystemRelation(
            id=self.id,
            project_id=self.project_id,
            from_repo_id=self.from_repo_id,
            to_repo_id=self.to_repo_id,
            relation_type=EcosystemRelationType(self.relation_type),
            evidence=self.evidence or "",
            confidence=self.confidence if self.confidence is not None else 1.0,
            agent_id=self.agent_id,
            created_at=self.created_at,
        )

    @classmethod
    def from_pydantic(cls, p: EcosystemRelation) -> EcosystemRelationModel:
        """Create an ORM instance from a Pydantic model."""
        return cls(
            id=p.id,
            project_id=p.project_id,
            from_repo_id=p.from_repo_id,
            to_repo_id=p.to_repo_id,
            relation_type=p.relation_type.value,
            evidence=p.evidence,
            confidence=p.confidence,
            agent_id=p.agent_id,
            created_at=p.created_at,
        )


class EcosystemScanRunModel(Base):
    """扫描批次记录 ORM 模型。

    项目隔离：每个 project_id 拥有独立的扫描历史。
    历史数据 project_id=NULL，迁移时 backfill 至默认项目。
    """

    __tablename__ = "ecosystem_scan_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    strategy: Mapped[str] = mapped_column(String(20), nullable=False, default="incremental")
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    repos_added: Mapped[int] = mapped_column(Integer, default=0)
    repos_updated: Mapped[int] = mapped_column(Integer, default=0)
    repos_skipped: Mapped[int] = mapped_column(Integer, default=0)
    # v1.6.1 Phase 2: repos with actual metadata changes (topics/stars/desc/lang)
    metadata_changed_count: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    triggered_by: Mapped[str] = mapped_column(String(20), default="manual")
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    def to_pydantic(self) -> EcosystemScanRun:
        """Convert to Pydantic model."""
        return EcosystemScanRun(
            id=self.id,
            project_id=self.project_id,
            strategy=EcosystemScanStrategy(self.strategy),
            started_at=self.started_at,
            completed_at=self.completed_at,
            duration_seconds=self.duration_seconds or 0.0,
            repos_added=self.repos_added or 0,
            repos_updated=self.repos_updated or 0,
            repos_skipped=self.repos_skipped or 0,
            metadata_changed_count=self.metadata_changed_count or 0,
            errors=self.errors if isinstance(self.errors, list) else [],
            notes=self.notes or "",
            triggered_by=self.triggered_by or "manual",
            agent_id=self.agent_id,
        )

    @classmethod
    def from_pydantic(cls, p: EcosystemScanRun) -> EcosystemScanRunModel:
        """Create an ORM instance from a Pydantic model."""
        return cls(
            id=p.id,
            project_id=p.project_id,
            strategy=p.strategy.value,
            started_at=p.started_at,
            completed_at=p.completed_at,
            duration_seconds=p.duration_seconds,
            repos_added=p.repos_added,
            repos_updated=p.repos_updated,
            repos_skipped=p.repos_skipped,
            metadata_changed_count=p.metadata_changed_count,
            errors=p.errors,
            notes=p.notes,
            triggered_by=p.triggered_by,
            agent_id=p.agent_id,
        )


# ============================================================
# v1.5.0-A 新建表：状态快照 + 项目 ecosystem 配置
# ============================================================


class EcosystemRepoStatusSnapshotModel(Base):
    """每次 scan 的仓状态快照 ORM 模型 (append-only，永不清理)。

    项目隔离：每条快照属于发起 scan 的项目；同一仓在不同项目下有独立快照流。
    用于历史时间线展示 (stars 涨跌、活跃集进出、archived 切换)。
    """

    __tablename__ = "ecosystem_repo_status_snapshots"
    __table_args__ = (
        Index(
            "ix_eco_status_snap_repo_time",
            "repo_id",
            "snapshot_at",
        ),
        Index(
            "ix_eco_status_snap_scan_run",
            "scan_run_id",
        ),
        Index(
            "ix_eco_status_snap_project_repo",
            "project_id",
            "repo_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    repo_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    scan_run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )
    stars: Mapped[int] = mapped_column(Integer, default=0)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    summary_at_time: Mapped[str] = mapped_column(Text, default="")

    def to_pydantic(self) -> EcosystemRepoStatusSnapshot:
        """Convert to Pydantic model."""
        return EcosystemRepoStatusSnapshot(
            id=self.id,
            project_id=self.project_id,
            repo_id=self.repo_id,
            scan_run_id=self.scan_run_id,
            snapshot_at=self.snapshot_at,
            stars=self.stars or 0,
            pushed_at=self.pushed_at,
            is_archived=bool(self.is_archived) if self.is_archived is not None else False,
            is_active=bool(self.is_active) if self.is_active is not None else True,
            summary_at_time=self.summary_at_time or "",
        )

    @classmethod
    def from_pydantic(cls, p: EcosystemRepoStatusSnapshot) -> EcosystemRepoStatusSnapshotModel:
        """Create an ORM instance from a Pydantic model."""
        return cls(
            id=p.id,
            project_id=p.project_id,
            repo_id=p.repo_id,
            scan_run_id=p.scan_run_id,
            snapshot_at=p.snapshot_at,
            stars=p.stars,
            pushed_at=p.pushed_at,
            is_archived=p.is_archived,
            is_active=p.is_active,
            summary_at_time=p.summary_at_time or "",
        )


class EcosystemProjectSettingsModel(Base):
    """每个项目的 ecosystem 配置 ORM 模型。

    project_id 主键 = 一项目一行。包含活跃集阈值、刷新间隔、关注白名单、并发参数。
    """

    __tablename__ = "ecosystem_project_settings"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    min_stars: Mapped[int] = mapped_column(Integer, default=1000)
    top_n: Mapped[int] = mapped_column(Integer, default=200)
    refresh_interval_days: Mapped[int] = mapped_column(Integer, default=7)
    auto_shallow_on_archive: Mapped[bool] = mapped_column(Boolean, default=True)
    focus_topics: Mapped[list[str]] = mapped_column(JSON, default=list)
    focus_languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    shallow_concurrency: Mapped[int] = mapped_column(Integer, default=5)
    deep_concurrency: Mapped[int] = mapped_column(Integer, default=3)
    # v1.6.1 Phase 2: migrated from scan_profile.alert_thresholds.max_new_per_scan
    alert_max_new_per_scan: Mapped[int] = mapped_column(Integer, default=50)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )

    def to_pydantic(self) -> EcosystemProjectSettings:
        """Convert to Pydantic model."""
        return EcosystemProjectSettings(
            project_id=self.project_id,
            min_stars=self.min_stars if self.min_stars is not None else 1000,
            top_n=self.top_n if self.top_n is not None else 200,
            refresh_interval_days=self.refresh_interval_days if self.refresh_interval_days is not None else 7,
            auto_shallow_on_archive=bool(self.auto_shallow_on_archive)
            if self.auto_shallow_on_archive is not None
            else True,
            focus_topics=self.focus_topics if isinstance(self.focus_topics, list) else [],
            focus_languages=self.focus_languages if isinstance(self.focus_languages, list) else [],
            shallow_concurrency=self.shallow_concurrency if self.shallow_concurrency is not None else 5,
            deep_concurrency=self.deep_concurrency if self.deep_concurrency is not None else 3,
            alert_max_new_per_scan=self.alert_max_new_per_scan if self.alert_max_new_per_scan is not None else 50,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_pydantic(cls, p: EcosystemProjectSettings) -> EcosystemProjectSettingsModel:
        """Create an ORM instance from a Pydantic model."""
        return cls(
            project_id=p.project_id,
            min_stars=p.min_stars,
            top_n=p.top_n,
            refresh_interval_days=p.refresh_interval_days,
            auto_shallow_on_archive=p.auto_shallow_on_archive,
            focus_topics=p.focus_topics,
            focus_languages=p.focus_languages,
            shallow_concurrency=p.shallow_concurrency,
            deep_concurrency=p.deep_concurrency,
            alert_max_new_per_scan=p.alert_max_new_per_scan,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


# ============================================================
# v1.6.0 P0: Multi-source data source + scan profile tables
# ============================================================


class EcosystemDataSourceModel(Base):
    """Data source configurations — one project can have multiple sources.

    @deprecated v1.6.1 — multi-source expansion denied (GitHub-only confirmed by user).
    Table retained to avoid data loss. Code no longer writes to this table.
    Use EcosystemProjectSettings for configuration.
    """

    __tablename__ = "ecosystem_data_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )

    def to_pydantic(self) -> DataSource:
        return DataSource(
            id=self.id,
            project_id=self.project_id,
            kind=DataSourceKind(self.kind),
            name=self.name,
            config=self.config_json or {},
            enabled=self.enabled,
            version=self.version or 1,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_pydantic(cls, ds: DataSource) -> EcosystemDataSourceModel:
        return cls(
            id=ds.id,
            project_id=ds.project_id,
            kind=ds.kind.value,
            name=ds.name,
            config_json=ds.config,
            enabled=ds.enabled,
            version=ds.version,
            created_at=ds.created_at,
            updated_at=ds.updated_at,
        )


class EcosystemScanProfileModel(Base):
    """Scan profile — versioned config for active/inactive/archive thresholds.

    Each project has at most one is_active=True row; older versions are kept for history.

    @deprecated v1.6.1 — alert_thresholds.max_new_per_scan migrated to
    EcosystemProjectSettings.alert_max_new_per_scan. Table retained to avoid data loss.
    MCP tools ecosystem_scan_profile_update and ecosystem_data_source_create now return
    deprecated stubs. Code reads this table only for backward-compat fallback in index_update.
    """

    __tablename__ = "ecosystem_scan_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(tz=UTC)
    )

    def to_pydantic(self) -> ScanProfile:
        return ScanProfile(
            id=self.id,
            project_id=self.project_id,
            version=self.version or 1,
            profile=self.profile_json or {},
            is_active=self.is_active,
            created_at=self.created_at,
        )

    @classmethod
    def from_pydantic(cls, sp: ScanProfile) -> EcosystemScanProfileModel:
        return cls(
            id=sp.id,
            project_id=sp.project_id,
            version=sp.version,
            profile_json=sp.profile,
            is_active=sp.is_active,
            created_at=sp.created_at,
        )


# ============================================================
# v1.6.0 P0.4: Index diff + status change tables
# ============================================================


class EcosystemIndexDiffModel(Base):
    """Stores the diff output of each index_update run."""

    __tablename__ = "ecosystem_index_diffs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scan_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    diff_type: Mapped[str] = mapped_column(String(20), default="incremental")
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    reactivated_count: Mapped[int] = mapped_column(Integer, default=0)
    deactivated_count: Mapped[int] = mapped_column(Integer, default=0)
    stale_count: Mapped[int] = mapped_column(Integer, default=0)
    archived_count: Mapped[int] = mapped_column(Integer, default=0)  # deprecated: use github_archived_changed_count
    # v1.6.0-P1 hotfix: new semantically-correct columns
    github_archived_changed_count: Mapped[int] = mapped_column(Integer, default=0)
    removed_from_query_count: Mapped[int] = mapped_column(Integer, default=0)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    markdown_summary: Mapped[str] = mapped_column(Text, default="")
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    def to_pydantic(self) -> EcosystemIndexDiff:
        import json

        details = {}
        if self.details_json:
            try:
                details = json.loads(self.details_json)
            except Exception:
                pass
        # Read new columns; fall back to old archived_count for rows written before P1 hotfix
        github_archived = getattr(self, "github_archived_changed_count", None) or self.archived_count
        removed_from_query = getattr(self, "removed_from_query_count", None) or 0
        return EcosystemIndexDiff(
            id=self.id,
            scan_run_id=self.scan_run_id,
            project_id=self.project_id,
            diff_type=self.diff_type,
            new_count=self.new_count,
            reactivated_count=self.reactivated_count,
            deactivated_count=self.deactivated_count,
            stale_count=self.stale_count,
            archived_count=self.archived_count,
            github_archived_changed_count=github_archived,
            removed_from_query_count=removed_from_query,
            details_json=details,
            markdown_summary=self.markdown_summary or "",
            alerted=self.alerted or False,
            generated_at=self.generated_at or datetime.now(UTC),
        )

    @classmethod
    def from_pydantic(cls, diff: EcosystemIndexDiff) -> EcosystemIndexDiffModel:
        import json

        return cls(
            id=diff.id,
            scan_run_id=diff.scan_run_id,
            project_id=diff.project_id,
            diff_type=diff.diff_type,
            new_count=diff.new_count,
            reactivated_count=diff.reactivated_count,
            deactivated_count=diff.deactivated_count,
            stale_count=diff.stale_count,
            archived_count=0,  # deprecated: always write 0 going forward
            github_archived_changed_count=diff.github_archived_changed_count,
            removed_from_query_count=diff.removed_from_query_count,
            details_json=json.dumps(diff.details_json),
            markdown_summary=diff.markdown_summary,
            alerted=diff.alerted,
            generated_at=diff.generated_at,
        )


class EcosystemStatusChangeModel(Base):
    """Tracks repo status transitions triggered by index_update runs."""

    __tablename__ = "ecosystem_status_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repo_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    scan_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    def to_pydantic(self) -> EcosystemStatusChange:
        return EcosystemStatusChange(
            id=self.id,
            repo_id=self.repo_id,
            project_id=self.project_id,
            from_status=self.from_status,
            to_status=self.to_status,
            scan_run_id=self.scan_run_id,
            reason=self.reason or "",
            triggered_at=self.triggered_at or datetime.now(UTC),
        )

    @classmethod
    def from_pydantic(cls, sc: EcosystemStatusChange) -> EcosystemStatusChangeModel:
        return cls(
            id=sc.id,
            repo_id=sc.repo_id,
            project_id=sc.project_id,
            from_status=sc.from_status,
            to_status=sc.to_status,
            scan_run_id=sc.scan_run_id,
            reason=sc.reason,
            triggered_at=sc.triggered_at,
        )


class EcosystemRepoEventModel(Base):
    """Full event log for every operation on an ecosystem repo (v1.6.0 event sourcing)."""

    __tablename__ = "ecosystem_repo_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repo_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="scanner")
    scan_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
    )

    def to_pydantic(self) -> EcosystemRepoEvent:
        import json

        payload: dict[str, Any] = {}
        if self.payload_json:
            try:
                payload = json.loads(self.payload_json)
            except Exception:
                pass
        return EcosystemRepoEvent(
            id=self.id,
            repo_id=self.repo_id,
            project_id=self.project_id,
            event_type=self.event_type,
            payload_json=payload,
            source=self.source or "scanner",
            scan_run_id=self.scan_run_id,
            from_status=self.from_status,
            to_status=self.to_status,
            reason=self.reason,
            triggered_at=self.triggered_at or datetime.now(UTC),
        )

    @classmethod
    def from_pydantic(cls, ev: EcosystemRepoEvent) -> EcosystemRepoEventModel:
        import json

        return cls(
            id=ev.id,
            repo_id=ev.repo_id,
            project_id=ev.project_id,
            event_type=ev.event_type,
            payload_json=json.dumps(ev.payload_json) if ev.payload_json else None,
            source=ev.source,
            scan_run_id=ev.scan_run_id,
            from_status=ev.from_status,
            to_status=ev.to_status,
            reason=ev.reason,
            triggered_at=ev.triggered_at,
        )


# ============================================================
# v1.7.0: Shallow batch management table
# ============================================================


class EcosystemShallowBatchModel(Base):
    """浅扫批次 — 聚合一次批量浅扫的元信息与候选仓快照。

    一个批次对应一轮候选仓的发现与分发流程，支持人工审批门控。
    """

    __tablename__ = "ecosystem_shallow_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    triggered_by: Mapped[str] = mapped_column(String(50))  # 'cron' / 'manual' / 'user'
    trigger_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidates_count: Mapped[int] = mapped_column(Integer, default=0)
    candidates_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of repo_id
    status: Mapped[str] = mapped_column(String(20), default="pending_approval")
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_repos_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_repos_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_changed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(tz=UTC)
    )

    def to_pydantic(self) -> EcosystemShallowBatch:
        return EcosystemShallowBatch(
            id=self.id,
            project_id=self.project_id,
            triggered_by=self.triggered_by,
            trigger_reason=self.trigger_reason,
            candidates_count=self.candidates_count or 0,
            candidates_snapshot_json=self.candidates_snapshot_json,
            status=self.status or "pending_approval",
            approved_by=self.approved_by,
            approved_at=self.approved_at,
            completed_at=self.completed_at,
            new_repos_count=self.new_repos_count or 0,
            updated_repos_count=self.updated_repos_count or 0,
            metadata_changed_count=self.metadata_changed_count or 0,
            failed_count=self.failed_count or 0,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_pydantic(cls, b: EcosystemShallowBatch) -> EcosystemShallowBatchModel:
        return cls(
            id=b.id,
            project_id=b.project_id,
            triggered_by=b.triggered_by,
            trigger_reason=b.trigger_reason,
            candidates_count=b.candidates_count,
            candidates_snapshot_json=b.candidates_snapshot_json,
            status=b.status,
            approved_by=b.approved_by,
            approved_at=b.approved_at,
            completed_at=b.completed_at,
            new_repos_count=b.new_repos_count,
            updated_repos_count=b.updated_repos_count,
            metadata_changed_count=b.metadata_changed_count,
            failed_count=b.failed_count,
            created_at=b.created_at,
            updated_at=b.updated_at,
        )


# ============================================================
# I3a: Workflow observability tables (可变投影 + append-only 审计分离)
#
# 两张表定位为「不可变文件 wf_<id>.json 的可重建缓存」：按自然键 UPSERT
# 单调推进 running→completed、绝不 DELETE 行。零迁移落地——create_all 自动
# 建缺失表（connection.py:539），不进 COLUMNS_TO_ENSURE。project_id 带
# String(36)+index 即自动获得 _apply_project_filter 支持。
# ============================================================


class WorkflowRunModel(Base):
    """Workflow 运行档案表 — 一个 wf_id 一行，upsert by wf_id。"""

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    wf_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=True, default="", index=True)
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cc_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="planned", index=True)
    source: Mapped[str] = mapped_column(String(20), default="hook")
    phases: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    planned_agent_count: Mapped[int] = mapped_column(Integer, default=0)
    dynamic_nodes: Mapped[int] = mapped_column(Integer, default=0)
    agent_count: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    script_path: Mapped[str] = mapped_column(String(500), default="")
    # 跨项目修复A：回执 transcript_dir 持久化（既有文件库经 COLUMNS_TO_ENSURE 补列）
    transcript_dir: Mapped[str] = mapped_column(String(500), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Phase2 live 水位列（既有文件库经 COLUMNS_TO_ENSURE ALTER 补齐）
    journal_offset: Mapped[int] = mapped_column(Integer, default=0)
    source_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    live_tokens: Mapped[int] = mapped_column(Integer, default=0)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> WorkflowRun:
        """Convert to Pydantic model."""
        return WorkflowRun(
            id=self.id,
            wf_id=self.wf_id,
            project_id=self.project_id or "",
            team_id=self.team_id,
            session_id=self.session_id,
            cc_task_id=self.cc_task_id,
            name=self.name or "",
            status=self.status or "planned",
            source=self.source or "hook",
            phases=self.phases if isinstance(self.phases, list) else [],
            planned_agent_count=self.planned_agent_count or 0,
            dynamic_nodes=self.dynamic_nodes or 0,
            agent_count=self.agent_count or 0,
            total_tokens=self.total_tokens or 0,
            total_tool_calls=self.total_tool_calls or 0,
            duration_ms=self.duration_ms,
            summary=self.summary or "",
            result=self.result if isinstance(self.result, dict) else None,
            script_path=self.script_path or "",
            transcript_dir=self.transcript_dir or "",
            started_at=self.started_at,
            completed_at=self.completed_at,
            journal_offset=self.journal_offset or 0,
            source_fingerprint=self.source_fingerprint or "",
            live_tokens=self.live_tokens or 0,
            last_activity_at=self.last_activity_at,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @staticmethod
    def from_pydantic(run: WorkflowRun) -> WorkflowRunModel:
        """Create an ORM instance from a Pydantic model."""
        return WorkflowRunModel(
            id=run.id,
            wf_id=run.wf_id,
            project_id=run.project_id or "",
            team_id=run.team_id,
            session_id=run.session_id,
            cc_task_id=run.cc_task_id,
            name=run.name,
            status=run.status,
            source=run.source,
            phases=run.phases,
            planned_agent_count=run.planned_agent_count,
            dynamic_nodes=run.dynamic_nodes,
            agent_count=run.agent_count,
            total_tokens=run.total_tokens,
            total_tool_calls=run.total_tool_calls,
            duration_ms=run.duration_ms,
            summary=run.summary,
            result=run.result,
            script_path=run.script_path,
            transcript_dir=run.transcript_dir,
            started_at=run.started_at,
            completed_at=run.completed_at,
            journal_offset=run.journal_offset or 0,
            source_fingerprint=run.source_fingerprint or "",
            live_tokens=run.live_tokens or 0,
            last_activity_at=run.last_activity_at,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )


class KnowledgeLinkModel(Base):
    """跨域引用边 — 知识层 P1a。append-only，UNIQUE 五元组去重（重复插入忽略）。"""

    __tablename__ = "knowledge_links"
    __table_args__ = (
        UniqueConstraint(
            "from_kind", "from_id", "to_kind", "to_id", "link_type",
            name="uq_klinks_edge",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_kind: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    from_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    to_kind: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    to_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    link_type: Mapped[str] = mapped_column(String(30), default="references")
    context: Mapped[str] = mapped_column(Text, default="")
    link_source: Mapped[str] = mapped_column(String(30), default="")
    project_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> KnowledgeLink:
        return KnowledgeLink(
            id=self.id,
            from_kind=self.from_kind,
            from_id=self.from_id,
            to_kind=self.to_kind,
            to_id=self.to_id,
            link_type=self.link_type or "references",
            context=self.context or "",
            link_source=self.link_source or "",
            project_id=self.project_id or "",
            created_at=self.created_at,
        )


class WorkflowAgentModel(Base):
    """逐-agent 遥测表 — 一个 run 一个 fan-out agent 一行，upsert by (wf_id, cc_agent_id)。"""

    __tablename__ = "workflow_agents"
    __table_args__ = (
        UniqueConstraint("wf_id", "cc_agent_id", name="uq_workflow_agents_wf_cc"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    wf_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=True, default="", index=True)
    cc_agent_id: Mapped[str] = mapped_column(String(100), index=True, default="")
    os_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    phase_index: Mapped[int] = mapped_column(Integer, default=0)
    phase_title: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(String(20), default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_tool_name: Mapped[str] = mapped_column(String(100), default="")
    last_tool_summary: Mapped[str] = mapped_column(Text, default="")
    prompt_preview: Mapped[str] = mapped_column(Text, default="")
    result_preview: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Phase2: agent jsonl 的 mtime（live 泳道右端 + 逐 agent 跳过水位）
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> WorkflowAgent:
        """Convert to Pydantic model."""
        return WorkflowAgent(
            id=self.id,
            run_id=self.run_id,
            wf_id=self.wf_id,
            project_id=self.project_id or "",
            cc_agent_id=self.cc_agent_id or "",
            os_agent_id=self.os_agent_id,
            label=self.label or "",
            phase_index=self.phase_index or 0,
            phase_title=self.phase_title or "",
            model=self.model or "",
            state=self.state or "",
            tokens=self.tokens or 0,
            tool_calls=self.tool_calls or 0,
            duration_ms=self.duration_ms,
            last_tool_name=self.last_tool_name or "",
            last_tool_summary=self.last_tool_summary or "",
            prompt_preview=self.prompt_preview or "",
            result_preview=self.result_preview or "",
            started_at=self.started_at,
            queued_at=self.queued_at,
            last_activity_at=self.last_activity_at,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @staticmethod
    def from_pydantic(agent: WorkflowAgent) -> WorkflowAgentModel:
        """Create an ORM instance from a Pydantic model."""
        return WorkflowAgentModel(
            id=agent.id,
            run_id=agent.run_id,
            wf_id=agent.wf_id,
            project_id=agent.project_id or "",
            cc_agent_id=agent.cc_agent_id,
            os_agent_id=agent.os_agent_id,
            label=agent.label,
            phase_index=agent.phase_index,
            phase_title=agent.phase_title,
            model=agent.model,
            state=agent.state,
            tokens=agent.tokens,
            tool_calls=agent.tool_calls,
            duration_ms=agent.duration_ms,
            last_tool_name=agent.last_tool_name,
            last_tool_summary=agent.last_tool_summary,
            prompt_preview=agent.prompt_preview,
            result_preview=agent.result_preview,
            started_at=agent.started_at,
            queued_at=agent.queued_at,
            last_activity_at=agent.last_activity_at,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
        )


class GovernanceLeaseModel(Base):
    """治理 leader 租约 — 单行表（id 恒为 'governance'）。

    多 API 实例并存时（审计 M50），仅租约持有者运行后台治理（reaper/watchdog
    的回收/推进/调度/唤醒/对账），杜绝重复唤醒与双份治理。DB 原子认领是本项目
    唯一获准的跨进程仲裁原语（文件锁四次翻车史，见 docs/design-archaeology）。
    时间戳存 tz-aware UTC ISO 字符串、只做字典序比较，刻意不经 ORM DateTime 解析。
    """

    __tablename__ = "governance_lease"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    holder: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(64), default="")
