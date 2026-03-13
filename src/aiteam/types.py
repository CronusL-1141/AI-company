"""AI Team OS — 全局共享类型定义.

所有模块引用此文件中的类型，不自行定义数据模型。
此文件由 tech-lead 统一管理，其他工程师只读引用。
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
# 枚举类型
# ============================================================


class OrchestrationMode(str, enum.Enum):
    """团队编排模式."""

    COORDINATE = "coordinate"
    BROADCAST = "broadcast"
    ROUTE = "route"
    MEET = "meet"


class TaskStatus(str, enum.Enum):
    """任务状态."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentStatus(str, enum.Enum):
    """Agent状态."""

    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    OFFLINE = "offline"
    RECOVERING = "recovering"


class MeetingStatus(str, enum.Enum):
    """会议状态."""

    ACTIVE = "active"
    CONCLUDED = "concluded"


class MemoryScope(str, enum.Enum):
    """记忆作用域."""

    GLOBAL = "global"
    TEAM = "team"
    AGENT = "agent"
    USER = "user"


class EventType(str, enum.Enum):
    """系统事件类型."""

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

    # System events
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPED = "system.stopped"
    SYSTEM_ERROR = "system.error"


# ============================================================
# 数据模型
# ============================================================


def _new_id() -> str:
    return str(uuid4())


class Team(BaseModel):
    """团队数据模型."""

    id: str = Field(default_factory=_new_id)
    name: str
    mode: OrchestrationMode = OrchestrationMode.COORDINATE
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class Agent(BaseModel):
    """Agent数据模型."""

    id: str = Field(default_factory=_new_id)
    team_id: str
    name: str
    role: str
    system_prompt: str = ""
    model: str = "claude-opus-4-6"
    status: AgentStatus = AgentStatus.IDLE
    config: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"  # "api" = CLAUDE.md主动注册, "hook" = hooks自动捕获
    session_id: str | None = None  # 关联的CC会话ID
    cc_tool_use_id: str | None = None  # 关联CC内部agent ID
    current_task: str | None = None  # 当前正在执行的任务/活动描述
    created_at: datetime = Field(default_factory=datetime.now)
    last_active_at: datetime | None = None


class Task(BaseModel):
    """任务数据模型."""

    id: str = Field(default_factory=_new_id)
    team_id: str
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    assigned_to: str | None = None
    result: str | None = None
    parent_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Memory(BaseModel):
    """记忆数据模型."""

    id: str = Field(default_factory=_new_id)
    scope: MemoryScope
    scope_id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    accessed_at: datetime = Field(default_factory=datetime.now)


class Event(BaseModel):
    """系统事件数据模型."""

    id: str = Field(default_factory=_new_id)
    type: EventType
    source: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)


class Meeting(BaseModel):
    """会议数据模型."""

    id: str = Field(default_factory=_new_id)
    team_id: str
    topic: str
    status: MeetingStatus = MeetingStatus.ACTIVE
    participants: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    concluded_at: datetime | None = None


class MeetingMessage(BaseModel):
    """会议消息数据模型."""

    id: str = Field(default_factory=_new_id)
    meeting_id: str
    agent_id: str
    agent_name: str
    content: str
    round_number: int = 1
    timestamp: datetime = Field(default_factory=datetime.now)


class AgentActivity(BaseModel):
    """Agent活动记录——记录agent的每次工具调用."""

    id: str = Field(default_factory=_new_id)
    agent_id: str
    session_id: str
    tool_name: str          # 工具名称 (Bash, Edit, Read, Agent等)
    input_summary: str = ""  # 输入摘要 (如命令、文件路径)
    output_summary: str = ""  # 输出摘要 (截断到500字符)
    timestamp: datetime = Field(default_factory=datetime.now)


# ============================================================
# 结果类型
# ============================================================


class TaskResult(BaseModel):
    """任务执行结果."""

    task_id: str
    status: TaskStatus
    result: str
    agent_outputs: dict[str, str] = Field(default_factory=dict)
    duration_seconds: float = 0.0


class TeamStatus(BaseModel):
    """团队状态摘要."""

    team: Team
    agents: list[Agent]
    active_tasks: list[Task]
    completed_tasks: int = 0
    total_tasks: int = 0


# ============================================================
# LangGraph 状态类型
# ============================================================


class TeamState(dict):
    """LangGraph StateGraph 的状态定义.

    使用TypedDict风格，但通过dict基类兼容LangGraph。
    """

    pass


# TeamState 的字段定义（用于 StateGraph 的 channels）
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
