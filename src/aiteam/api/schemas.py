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
    # 默认留空：模型未知就不落具体型号（由 transcript 观测回填），
    # 具体版本写死在默认值里必然过时（4.7 残留即此类）——2026-07-07 立规
    model: str = ""


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
    meta_json: dict = Field(default_factory=dict)


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
    supersedes: str | None = None  # 记忆 v2：被本条取代的旧 memo id（置其失效）


class MemoryCreate(BaseModel):
    """方向层记忆写入请求（记忆系统 v2 P1）。

    scope_id 可留空由服务层按 scope 推导：global→"system"、user→"user"、
    project→当前项目 id（X-Project-Id / X-Project-Dir）。
    """

    content: str
    kind: str = "preference"  # constraint / design / directive / preference
    scope: str = "global"  # global / project / user
    scope_id: str = ""
    source_refs: list[str] = Field(default_factory=list)  # 溯源：memo/report/meeting id
    supersedes: str | None = None  # 被本条置换失效的旧 memory id


class MemoryInvalidate(BaseModel):
    """方向层记忆显式失效请求。"""

    invalidated_by: str | None = None  # 取代者 memory id（可选）


class ReconcileOperation(BaseModel):
    """记忆整理单条操作（记忆系统 v2 P2）。

    op 语义（agent LLM 精判后提交，工具只做确定性应用）：
    - merge：content + memo_ids → 建新 memo、被并各条置失效（invalidated_by=新条）
    - invalidate：memo_ids → 逐条失效（矛盾/被推翻）
    - score：memo_id + quality_score(1-10) + reason → 补质量分（reason 入 meta）
    - promote：content + kind + source_refs → 建方向层条目（体量红线照常生效）
    - keep / noop：不动（可省略不提交）
    """

    op: str  # merge / invalidate / score / promote / keep / noop
    content: str = ""  # merge/promote 的新内容
    memo_ids: list[str] = Field(default_factory=list)  # merge/invalidate 的目标 memo
    memo_id: str = ""  # score 的目标 memo
    quality_score: int | None = None  # score：1-10
    reason: str = ""  # score 的评分理由
    kind: str = "preference"  # promote 的方向层 kind
    scope: str = "project"  # promote 的方向层 scope
    source_refs: list[str] = Field(default_factory=list)  # promote 的溯源 id
    memo_type: str = "summary"  # merge 新条的 memo_type
    scope_path: str = ""  # merge 新条的 scope_path


class ReconcileApply(BaseModel):
    """记忆整理批量应用请求。"""

    operations: list[ReconcileOperation] = Field(default_factory=list)


class MeetingMessageCreate(BaseModel):
    """Create meeting message request."""

    agent_id: str
    agent_name: str
    content: str
    round_number: int = 1
    caller_agent_id: str = ""  # actual caller; if differs from agent_id → impersonation audit


class MeetingConcludeBody(BaseModel):
    """Conclude meeting request body."""

    summary: str = ""
    validate_attendance: bool = True
    force: bool = False


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
