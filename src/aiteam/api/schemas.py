"""AI Team OS — API request/response schemas.

Defines unified response wrappers and request models.
Response data fields reuse Pydantic models from types.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from aiteam.types import LongText, MemoType, SingleLineText

T = TypeVar("T")


# ============================================================
# Unified response wrappers
# ============================================================


class APIResponse(BaseModel, Generic[T]):
    """Unified API response."""

    success: bool = True
    data: T | None = None
    message: str = ""


class EcosystemWritebackHint(BaseModel):
    """Ecosystem writeback reminder carried by a meeting_conclude result."""

    review_ids: list[str] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    next_step: str = ""


class MeetingConcludeResponse(APIResponse[T], Generic[T]):
    """Conclude result: the meeting, plus a writeback hint when ecosystem signals are present.

    ``ecosystem_writeback`` is null unless deep reviews link this meeting or its
    topic carries an ecosystem keyword.
    """

    ecosystem_writeback: EcosystemWritebackHint | None = None


class APIListResponse(BaseModel, Generic[T]):
    """Unified list response."""

    success: bool = True
    data: list[T] = Field(default_factory=list)
    total: int = 0
    message: str = ""


# ============================================================
# Request models
# ============================================================


class TeamUpdate(BaseModel):
    """Update team request."""

    mode: str | None = None
    status: str | None = None
    # 项目归属改派（2026-07-26 补）：此前无任何接口可改 team.project_id——自动
    # 归属一旦推错（hook 兜底猜错项目）就永久卡死，队在错的项目视图里显示。
    # 传空字符串表示解绑（回到未归属，等下次权威路径重新绑定）。
    project_id: str | None = None


class AgentCreate(BaseModel):
    """Create Agent request."""

    name: SingleLineText
    role: SingleLineText
    system_prompt: LongText = ""
    # 默认留空：模型未知就不落具体型号（由 transcript 观测回填），
    # 具体版本写死在默认值里必然过时（4.7 残留即此类）——2026-07-07 立规
    model: str = ""


class TaskRun(BaseModel):
    """Run task request."""

    description: LongText
    title: SingleLineText = ""
    model: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    priority: str = "medium"
    horizon: str = "short"
    tags: list[SingleLineText] = Field(default_factory=list)
    assigned_to: SingleLineText | None = None


class MemoryQuery(BaseModel):
    """Memory query request."""

    scope: str = "global"
    scope_id: str = "system"
    query: str = ""
    limit: int = 10


class AgentStatusUpdate(BaseModel):
    """Update Agent status request."""

    status: str
    current_task: SingleLineText | None = None


class ProjectCreate(BaseModel):
    """Create project request."""

    name: SingleLineText
    root_path: str = ""
    description: LongText = ""
    config: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    """Update project request."""

    name: SingleLineText | None = None
    root_path: str | None = None
    description: LongText | None = None
    config: dict[str, Any] | None = None


class PhaseCreate(BaseModel):
    """Create phase request."""

    name: SingleLineText
    description: LongText = ""
    order: int = 0
    config: dict[str, Any] = Field(default_factory=dict)


class PhaseStatusUpdate(BaseModel):
    """Update phase status request."""

    status: str


class MeetingCreate(BaseModel):
    """Create meeting request."""

    topic: SingleLineText
    participants: list[SingleLineText] = Field(default_factory=list)
    meta_json: dict = Field(default_factory=dict)


class SubtaskInput(BaseModel):
    """Subtask input."""

    title: SingleLineText
    description: LongText = ""


class TaskDecompose(BaseModel):
    """Task decomposition request."""

    title: SingleLineText
    description: LongText = ""
    template: str = ""  # web-app/api-service/data-pipeline/library/refactor/bugfix
    subtasks: list[SubtaskInput] | None = None
    auto_assign: bool = False
    priority: str = "medium"
    horizon: str = "short"
    tags: list[SingleLineText] = Field(default_factory=list)


class TaskCreateBody(BaseModel):
    """Project-level task creation request."""

    title: SingleLineText
    description: LongText = ""
    priority: str = "medium"
    horizon: str = "mid"
    tags: list[SingleLineText] = Field(default_factory=list)
    assigned_to: SingleLineText | None = None
    status: str = "pending"
    # CC 桥接字段（桥 hook 已退役，接口保留）。镜像请求会带 assigned_to，此前这里
    # 没这个字段，Pydantic 默默丢掉——镜像出来的任务永远没有 owner。
    cc_task_id: str | None = None
    # CC 的 blockedBy（CC 自己的任务 id）。服务端把其中已镜像过的解析成 OS 的
    # depends_on，解析不到的原样留在 config 里存证——绝不把 CC 的 id 直接塞进
    # depends_on 冒充 OS 任务 id。
    cc_blocked_by: list[str] = Field(default_factory=list)


class TaskUpdateBody(BaseModel):
    """Partial update task request — all fields optional."""

    status: str | None = None
    assigned_to: SingleLineText | None = None
    result: LongText | None = None
    priority: str | None = None
    tags: list[SingleLineText] | None = None
    title: SingleLineText | None = None
    description: LongText | None = None


class IssueReport(BaseModel):
    """Report issue request."""

    title: SingleLineText
    description: LongText = ""
    severity: str = "medium"
    category: str = "bug"


class MemoEntry(BaseModel):
    """Task memo entry request."""

    author: SingleLineText = "leader"
    content: LongText
    type: MemoType = "progress"
    supersedes: str | None = None  # 记忆 v2：被本条取代的旧 memo id（置其失效）


class MemoryCreate(BaseModel):
    """方向层记忆写入请求（记忆系统 v2 P1）。

    scope_id 可留空由服务层按 scope 推导：global→"system"、user→"user"、
    project→当前项目 id（X-Project-Id / X-Project-Dir）。
    """

    content: LongText
    kind: str = "preference"  # constraint / design / directive / preference
    scope: str = "global"  # global / project / user
    scope_id: str = ""
    source_refs: list[str] = Field(default_factory=list)  # 溯源：memo/report/meeting id
    supersedes: str | None = None  # 被本条置换失效的旧 memory id
    # 置换 global/user 条目时须为 true（缔造者已过目）；project 桶的置换不需要
    confirm_shared_scope: bool = False


class MemoryInvalidate(BaseModel):
    """方向层记忆显式失效请求。"""

    invalidated_by: str | None = None  # 取代者 memory id（可选）
    # global/user 条目被所有项目的会话继承，失效须显式确认（缔造者过目后再带 true）
    confirm_shared_scope: bool = False


class MemoryInvalidateByMatch(BaseModel):
    """按内容子串定位并失效一条方向层记忆（v2.1 定位协议）。

    子串须唯一命中当前上下文的有效条目（global + user + 当前项目）；
    命中 0 条或多条一律不动数据。
    """

    content_match: str  # 唯一定位子串
    invalidated_by: str | None = None  # 取代者 memory id（可选）
    confirm_shared_scope: bool = False  # 命中 global/user 条目时须为 true


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
    memo_type: MemoType = "summary"  # merge 新条的 memo_type
    scope_path: str = ""  # merge 新条的 scope_path


class ReconcileApply(BaseModel):
    """记忆整理批量应用请求。"""

    operations: list[ReconcileOperation] = Field(default_factory=list)
    # candidates 发的整理权凭据；CC 会话内按 X-CC-Session-Id 自动识别，可不传
    lease_id: str = ""
    # 分批应用时非最后一批传 true：本批全部成功也不释放整理权
    keep_lease: bool = False


class MeetingMessageCreate(BaseModel):
    """Create meeting message request."""

    agent_id: str
    agent_name: SingleLineText
    content: LongText
    round_number: int = 1
    caller_agent_id: str = ""  # actual caller; if differs from agent_id → impersonation audit


class MeetingConcludeBody(BaseModel):
    """Conclude meeting request body."""

    summary: str = ""
    validate_attendance: bool = True
    force: bool = False


class ChannelMessageCreate(BaseModel):
    """Send channel message request."""

    sender: SingleLineText
    content: LongText
    # 裸名与 "@名" 都是合法书写，未读判定两种都认（见 types.ChannelMessage 的说明）
    mentions: list[SingleLineText] = Field(default_factory=list)  # e.g. ["agent-name"] / ["@agent-name"]
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 归属项目。channel 形如 "project:<id>" 时可省略（由路由从频道名解析）；其余频道
    # 必须显式给出，否则路由 400 拒收——留空的消息按项目查询时谁都看不到，这是比报错
    # 糟糕得多的失败形态。
    project_id: str | None = None


class ChannelCursorAdvance(BaseModel):
    """推进某个读者在某频道的已读水位。"""

    reader: str
    project_id: str
    # 取"本次实际读到的最后一条消息的 created_at"。服务端刻意不取 now：分页只拿了前
    # N 条时按 now 推进会静默跳过未返回的那些，用户再也不会被提示。
    last_read_at: datetime
