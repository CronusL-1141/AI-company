"""AI Team OS — SQLAlchemy ORM 模型定义.

将 types.py 中的 Pydantic 模型映射为 SQLAlchemy 2.0 ORM 模型，
用于 SQLite 数据持久化。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aiteam.types import (
    Agent,
    AgentActivity,
    AgentStatus,
    Event,
    EventType,
    Meeting,
    MeetingMessage,
    MeetingStatus,
    Memory,
    MemoryScope,
    OrchestrationMode,
    Phase,
    PhaseStatus,
    Project,
    Task,
    TaskStatus,
    Team,
)

# ============================================================
# 基类
# ============================================================


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类."""

    pass


# ============================================================
# ORM 模型
# ============================================================


class ProjectModel(Base):
    """项目表."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    root_path: Mapped[str] = mapped_column(String(500), unique=True, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> Project:
        """转换为 Pydantic 模型."""
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
        """从 Pydantic 模型创建 ORM 实例."""
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
    """阶段表."""

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
        """转换为 Pydantic 模型."""
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
        """从 Pydantic 模型创建 ORM 实例."""
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
    """团队表."""

    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="coordinate")
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> Team:
        """转换为 Pydantic 模型."""
        return Team(
            id=self.id,
            name=self.name,
            mode=OrchestrationMode(self.mode),
            project_id=self.project_id,
            status=self.status or "active",
            config=self.config or {},
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @staticmethod
    def from_pydantic(team: Team) -> TeamModel:
        """从 Pydantic 模型创建 ORM 实例."""
        return TeamModel(
            id=team.id,
            name=team.name,
            mode=team.mode.value,
            project_id=team.project_id,
            status=team.status,
            config=team.config,
            created_at=team.created_at,
            updated_at=team.updated_at,
        )


class AgentModel(Base):
    """Agent表."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(100), default="claude-opus-4-6")
    status: Mapped[str] = mapped_column(String(20), default="idle")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(20), default="api")
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cc_tool_use_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    current_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_phase_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_pydantic(self) -> Agent:
        """转换为 Pydantic 模型."""
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
            created_at=self.created_at,
            last_active_at=self.last_active_at,
        )

    @staticmethod
    def from_pydantic(agent: Agent) -> AgentModel:
        """从 Pydantic 模型创建 ORM 实例."""
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
            created_at=agent.created_at,
            last_active_at=agent.last_active_at,
        )


class TaskModel(Base):
    """任务表."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    assigned_to: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_pydantic(self) -> Task:
        """转换为 Pydantic 模型."""
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
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
        )

    @staticmethod
    def from_pydantic(task: Task) -> TaskModel:
        """从 Pydantic 模型创建 ORM 实例."""
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
            created_at=task.created_at,
            started_at=task.started_at,
            completed_at=task.completed_at,
        )


class MemoryModel(Base):
    """记忆表."""

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    accessed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> Memory:
        """转换为 Pydantic 模型."""
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
        """从 Pydantic 模型创建 ORM 实例."""
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
    """事件表."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> Event:
        """转换为 Pydantic 模型."""
        return Event(
            id=self.id,
            type=EventType(self.type),
            source=self.source,
            data=self.data or {},
            timestamp=self.timestamp,
        )

    @staticmethod
    def from_pydantic(event: Event) -> EventModel:
        """从 Pydantic 模型创建 ORM 实例."""
        return EventModel(
            id=event.id,
            type=event.type.value,
            source=event.source,
            data=event.data,
            timestamp=event.timestamp,
        )


class MeetingModel(Base):
    """会议表."""

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
        """转换为 Pydantic 模型."""
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
        """从 Pydantic 模型创建 ORM 实例."""
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
    """会议消息表."""

    __tablename__ = "meeting_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    round_number: Mapped[int] = mapped_column(default=1)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> MeetingMessage:
        """转换为 Pydantic 模型."""
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
        """从 Pydantic 模型创建 ORM 实例."""
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
    """Agent活动记录表."""

    __tablename__ = "agent_activities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def to_pydantic(self) -> AgentActivity:
        """转换为 Pydantic 模型."""
        return AgentActivity(
            id=self.id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            tool_name=self.tool_name,
            input_summary=self.input_summary or "",
            output_summary=self.output_summary or "",
            timestamp=self.timestamp,
        )

    @staticmethod
    def from_pydantic(activity: AgentActivity) -> AgentActivityModel:
        """从 Pydantic 模型创建 ORM 实例."""
        return AgentActivityModel(
            id=activity.id,
            agent_id=activity.agent_id,
            session_id=activity.session_id,
            tool_name=activity.tool_name,
            input_summary=activity.input_summary,
            output_summary=activity.output_summary,
            timestamp=activity.timestamp,
        )
