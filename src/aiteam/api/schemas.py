"""AI Team OS — API request/response schemas.

Defines unified response wrappers and request models.
Response data fields reuse Pydantic models from types.py.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ============================================================
# Unified response wrappers
# ============================================================


class APIResponse(BaseModel, Generic[T]):
    """Unified API response."""

    success: bool = True
    data: T | None = None
    message: str = ""


class APIListResponse(BaseModel, Generic[T]):
    """Unified list response."""

    success: bool = True
    data: list[T] = Field(default_factory=list)
    total: int = 0
    message: str = ""


# ============================================================
# Request models
# ============================================================


class TeamCreate(BaseModel):
    """Create team request."""

    name: str
    mode: str = "coordinate"
    config: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = None
    leader_agent_id: str | None = None


class TeamUpdate(BaseModel):
    """Update team request."""

    mode: str | None = None
    status: str | None = None


class AgentCreate(BaseModel):
    """Create Agent request."""

    name: str
    role: str
    system_prompt: str = ""
    model: str = "claude-opus-4-6"


class TaskCreate(BaseModel):
    """Create task request."""

    title: str
    description: str = ""


class TaskRun(BaseModel):
    """Run task request."""

    description: str
    title: str = ""
    model: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    priority: str = "medium"
    horizon: str = "short"
    tags: list[str] = Field(default_factory=list)
    assigned_to: str | None = None


class MemoryQuery(BaseModel):
    """Memory query request."""

    scope: str = "global"
    scope_id: str = "system"
    query: str = ""
    limit: int = 10


class AgentStatusUpdate(BaseModel):
    """Update Agent status request."""

    status: str
    current_task: str | None = None


class ProjectCreate(BaseModel):
    """Create project request."""

    name: str
    root_path: str = ""
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    """Update project request."""

    name: str | None = None
    root_path: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None


class PhaseCreate(BaseModel):
    """Create phase request."""

    name: str
    description: str = ""
    order: int = 0
    config: dict[str, Any] = Field(default_factory=dict)


class PhaseStatusUpdate(BaseModel):
    """Update phase status request."""

    status: str


class MeetingCreate(BaseModel):
    """Create meeting request."""

    topic: str
    participants: list[str] = Field(default_factory=list)


class SubtaskInput(BaseModel):
    """Subtask input."""

    title: str
    description: str = ""


class TaskDecompose(BaseModel):
    """Task decomposition request."""

    title: str
    description: str = ""
    template: str = ""  # web-app/api-service/data-pipeline/library/refactor/bugfix
    subtasks: list[SubtaskInput] | None = None
    auto_assign: bool = False
    priority: str = "medium"
    horizon: str = "short"
    tags: list[str] = Field(default_factory=list)


class TaskCreateBody(BaseModel):
    """Project-level task creation request."""

    title: str
    description: str = ""
    priority: str = "medium"
    horizon: str = "mid"
    tags: list[str] = Field(default_factory=list)


class TaskUpdateBody(BaseModel):
    """Partial update task request — all fields optional."""

    status: str | None = None
    assigned_to: str | None = None
    result: str | None = None
    priority: str | None = None
    tags: list[str] | None = None
    title: str | None = None
    description: str | None = None


class IssueReport(BaseModel):
    """Report issue request."""

    title: str
    description: str = ""
    severity: str = "medium"
    category: str = "bug"


class MemoEntry(BaseModel):
    """Task memo entry request."""

    author: str = "leader"
    content: str
    type: str = "progress"  # progress / decision / issue / summary


class MeetingMessageCreate(BaseModel):
    """Create meeting message request."""

    agent_id: str
    agent_name: str
    content: str
    round_number: int = 1


class CrossMessageCreate(BaseModel):
    """Send cross-project message request."""

    to_project_id: str | None = None  # None = broadcast to all projects
    sender_name: str
    content: str
    message_type: str = "notification"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChannelMessageCreate(BaseModel):
    """Send channel message request."""

    sender: str
    content: str
    mentions: list[str] = Field(default_factory=list)  # e.g. ["@agent-name", "@team-name"]
    metadata: dict[str, Any] = Field(default_factory=dict)
