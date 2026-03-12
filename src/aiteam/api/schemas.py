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


class TeamUpdate(BaseModel):
    """更新团队请求."""

    mode: str | None = None


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


class MemoryQuery(BaseModel):
    """记忆查询请求."""

    scope: str = "global"
    scope_id: str = "system"
    query: str = ""
    limit: int = 10
