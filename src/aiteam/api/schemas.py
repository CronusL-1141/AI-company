"""AI Team OS — API请求/响应Schema.

定义统一响应包装和请求模型。Response的data字段复用types.py中的Pydantic模型。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# ============================================================
# 统一响应包装
# ============================================================


class APIResponse(BaseModel, Generic[T]):
    """统一API响应."""

    success: bool = True
    data: T | None = None
    message: str = ""


class APIListResponse(BaseModel, Generic[T]):
    """统一列表响应."""

    success: bool = True
    data: list[T] = Field(default_factory=list)
    total: int = 0
    message: str = ""


# ============================================================
# 请求模型
# ============================================================


class TeamCreate(BaseModel):
    """创建团队请求."""

    name: str
    mode: str = "coordinate"
    config: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = None
    leader_agent_id: str | None = None


class TeamUpdate(BaseModel):
    """更新团队请求."""

    mode: str | None = None
    status: str | None = None


class AgentCreate(BaseModel):
    """创建Agent请求."""

    name: str
    role: str
    system_prompt: str = ""
    model: str = "claude-opus-4-6"


class TaskCreate(BaseModel):
    """创建任务请求."""

    title: str
    description: str = ""


class TaskRun(BaseModel):
    """运行任务请求."""

    description: str
    title: str = ""
    model: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    priority: str = "medium"
    horizon: str = "short"
    tags: list[str] = Field(default_factory=list)
    assigned_to: str | None = None


class MemoryQuery(BaseModel):
    """记忆查询请求."""

    scope: str = "global"
    scope_id: str = "system"
    query: str = ""
    limit: int = 10


class AgentStatusUpdate(BaseModel):
    """更新Agent状态请求."""

    status: str
    current_task: str | None = None


class ProjectCreate(BaseModel):
    """创建项目请求."""

    name: str
    root_path: str = ""
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    """更新项目请求."""

    name: str | None = None
    root_path: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None


class PhaseCreate(BaseModel):
    """创建阶段请求."""

    name: str
    description: str = ""
    order: int = 0
    config: dict[str, Any] = Field(default_factory=dict)


class PhaseStatusUpdate(BaseModel):
    """更新阶段状态请求."""

    status: str


class MeetingCreate(BaseModel):
    """创建会议请求."""

    topic: str
    participants: list[str] = Field(default_factory=list)


class SubtaskInput(BaseModel):
    """子任务输入."""

    title: str
    description: str = ""


class TaskDecompose(BaseModel):
    """任务拆解请求."""

    title: str
    description: str = ""
    template: str = ""  # web-app/api-service/data-pipeline/library/refactor/bugfix
    subtasks: list[SubtaskInput] | None = None
    auto_assign: bool = False
    priority: str = "medium"
    horizon: str = "short"
    tags: list[str] = Field(default_factory=list)


class IssueReport(BaseModel):
    """上报问题请求."""

    title: str
    description: str = ""
    severity: str = "medium"
    category: str = "bug"


class MemoEntry(BaseModel):
    """任务Memo记录请求."""

    author: str = "leader"
    content: str
    type: str = "progress"  # progress / decision / issue / summary


class MeetingMessageCreate(BaseModel):
    """创建会议消息请求."""

    agent_id: str
    agent_name: str
    content: str
    round_number: int = 1
