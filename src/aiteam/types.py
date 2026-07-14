"""AI Team OS — Global shared type definitions.

All modules reference types from this file; they do not define their own data models.
This file is managed by the tech-lead; other engineers only read-reference it.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

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
    """Memory scope.

    方向层（记忆系统 v2 P1）语义收窄为 global / project / user——跨任务长寿命的
    偏好/纠正/约束/设计意图。task 级只影响单个任务的记录属情景层，走 task_memos。
    TEAM / AGENT 为历史遗留分区（团队知识库 / agent 经验），不属方向层。
    """

    GLOBAL = "global"
    PROJECT = "project"
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

    # Workflow observability events (I3a — CC ultracode/Workflow observation layer)
    # append-only: 一旦有历史数据写入不可再删（读端 EventType(x) 会崩）。
    WORKFLOW_PLANNED = "workflow.planned"  # PreToolUse(Workflow) 静态计划就绪
    WORKFLOW_STARTED = "workflow.started"  # PostToolUse(Workflow) 回执骨架就绪
    WORKFLOW_COMPLETED = "workflow.completed"  # 文件对账落最终遥测
    # Phase 2 live 追踪（兑现上方预留；每 run 每 tick 聚合发送，绝不逐 agent 逐条发）
    WORKFLOW_AGENT_UPDATED = "workflow.agent_updated"  # live tail：本 tick 有 agent 增量
    WORKFLOW_RUN_INGESTED = "workflow.run_ingested"  # run 级 live 水位 / killed·failed 首次终态 / interrupted 打标

    # 工具渐进式加载 P1 — alwaysLoad 动态轮换审计（会话启动期每次重算落一行；
    # 该行同时是下期迟滞基线，状态与审计合一。append-only，历史写入后不可删。）
    TOOL_ALWAYSLOAD_ROTATION = "tool.alwaysload.rotation"


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
    # 模型未知即留空（展示为 --）：默认烘焙具体型号曾在四层（此处/ORM 列默认/
    # to_pydantic 读注入/工具参数）反复冒出误导展示，真实值由 transcript 尾读
    # (Leader)/wf 终态(workflow agent)回填。
    model: str = ""
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
    # Agent reuse governance P1 (batch 1B): sub-agent context watermark ledger.
    # Populated from the sub-agent transcript on SubagentStop + reaper backfill;
    # reuse_domain is provisioned for the P2 decision layer (not written in P1).
    # See docs/agent-reuse-design.md section 4.
    ctx_tokens: int | None = None  # last measured context token total (D1 formula)
    ctx_window: int | None = None  # detected window size (e.g. 1_000_000)
    ctx_pct: float | None = None  # ctx_tokens / ctx_window * 100
    transcript_path: str | None = None  # sub-agent transcript pointer (resume/re-read anchor)
    ctx_measured_at: datetime | None = None  # when the watermark was last measured
    reuse_domain: str | None = None  # most-recent task domain tag (P2 decision layer)


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
    """Memory data model.

    方向层（记忆系统 v2 P1）：低频·高价值密度·跨任务长寿命的偏好/纠正/约束/
    设计意图。scope 语义 global/project/user；矛盾/更新时用 supersedes 置旧条失效
    （Zep 失效语义，不删除）。source_refs 回指 memo/report/meeting id（④溯源）。
    """

    id: str = Field(default_factory=_new_id)
    scope: MemoryScope
    scope_id: str
    content: str
    # preference(偏好) / directive(指令·工作方式) / constraint(约束) / design(设计意图)
    kind: str = "preference"
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)  # ④ 溯源：memo/report/meeting id
    invalid_at: datetime | None = None  # ① 失效轴（NULL=有效）
    invalidated_by: str | None = None  # 取代者 memory id
    created_at: datetime = Field(default_factory=datetime.now)
    accessed_at: datetime = Field(default_factory=datetime.now)


class TaskMemo(BaseModel):
    """情景层 task memo（记忆系统 v2 P0：从 tasks.config JSON 数组升为独立表）。

    行级真 ID（可被 knowledge_links 引用）+ 失效轴（invalid_at/invalidated_by，
    Zep 失效语义：矛盾时置失效不删除）。写入接口保持兼容，字段对齐设计 §2。
    """

    id: str = Field(default_factory=_new_id)
    task_id: str
    project_id: str | None = None
    author: str = "leader"
    memo_type: str = "progress"  # progress / decision / issue / summary
    content: str
    scope_path: str = ""  # ② 路径作用域 /project/ecosystem/research
    quality_score: int | None = None  # ⑧ 质量分（NULL=未评，整理时补）
    invalid_at: datetime | None = None  # ① 失效轴（NULL=有效）
    invalidated_by: str | None = None  # 取代者 memo id
    meta: dict[str, Any] = Field(default_factory=dict)  # entities/topics（整理时补）
    created_at: datetime = Field(default_factory=datetime.now)


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
    meta_json: dict[str, Any] = Field(default_factory=dict)
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
    msg_metadata: dict[str, Any] = Field(default_factory=dict)  # audit: impersonation, actual_author, etc.


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


class Report(BaseModel):
    """Research/analysis report — stored in database with project isolation."""

    id: str = Field(default_factory=_new_id)
    project_id: str = ""
    author: str = ""
    topic: str = ""
    report_type: str = "research"  # research / design / analysis / meeting-minutes
    date: str = ""  # YYYY-MM-DD
    content: str = ""
    task_id: str = ""
    team_id: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class WorkflowRun(BaseModel):
    """Workflow 运行档案 — CC ultracode/Workflow 一次运行的可查询投影。

    定位：`wf_<id>.json` 富快照的「可重建缓存」，按自然键 `wf_id` UPSERT 单调推进
    （planned→running→completed / interrupted），绝不删行。审计轨仍走 events 表。
    """

    id: str = Field(default_factory=_new_id)
    wf_id: str  # wf_<id>，幂等主锚
    project_id: str = ""  # 绑 launching Leader 项目，走 _apply_project_filter
    team_id: str | None = None  # 既有 workflow-<wf_id> 团队；OS 离线期无团队时留 None
    session_id: str | None = None  # 启动 Leader 会话
    cc_task_id: str | None = None  # 回执里的 Task ID（≠ OS task_id）
    name: str = ""  # run 名（回执/脚本 meta）
    status: str = "planned"  # planned / running / completed / interrupted / killed / failed
    source: str = "hook"  # 数据面溯源：hook / file / hook+file
    phases: list[dict[str, Any]] = Field(default_factory=list)  # [{index,title}]
    planned_agent_count: int = 0  # 静态解析 literal_agent_count
    dynamic_nodes: int = 0  # 静态解析动态节点数
    agent_count: int = 0  # 实际（快照 agentCount）
    total_tokens: int = 0  # 快照 totalTokens
    total_tool_calls: int = 0  # 快照 totalToolCalls
    duration_ms: int | None = None  # 快照 durationMs
    summary: str = ""  # run 结果摘要
    result: dict[str, Any] | None = None  # 终端 StructuredOutput（截断防膨胀）
    script_path: str = ""  # 脚本 .js 路径，供下钻
    # 跨项目修复A：回执 Transcript dir 持久化——live/终态直接寻址，摆脱「项目必须
    # 已注册」的依赖（未注册项目的 run 曾因 slug 扫不到而误判 interrupted/live 全盲）。
    transcript_dir: str = ""
    started_at: datetime | None = None  # startTime
    completed_at: datetime | None = None  # startTime + durationMs
    # Phase2 live 水位列 —— None=本次 upsert 不改；显式 0/''=复位（水位语义，
    # 见 repository.upsert_workflow_run 独立分支，绝不套「新非零胜出」）。
    journal_offset: int | None = None  # journal.jsonl 已消费字节水位（只前进到最后 \n）
    source_fingerprint: str | None = None  # wf_<id>.json 的 "mtime_ns:size"，reconcile 廉价跳过
    live_tokens: int | None = None  # 运行期估值 = Σ agents lastCtx（cached 记 0）；终态 UI 用 total_tokens
    last_activity_at: datetime | None = None  # max(journal+agent jsonl mtime)；单调取 max
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class KnowledgeLink(BaseModel):
    """跨域引用边 — 知识层 P1a（docs/knowledge-layer-design.md）。

    从 memo/report 文本用零 LLM 正则抽取 OS 原生 ID 引用（wf_id/commit/
    task-uuid/[[memory]]），append-only，UNIQUE 五元组去重。图谱=派生视图，
    可从源文本随时重建（文件真相源哲学）。
    """

    id: int | None = None  # 自增，插入前为 None
    from_kind: str  # task_memo / report / task
    from_id: str  # memo: "<task_id>#<ts>"; report/task: uuid
    to_kind: str  # run / task / commit / memory / report
    to_id: str  # wf_id / uuid / short-hash / memory-slug
    link_type: str = "references"  # references / fixes
    context: str = ""  # 命中点 ±120 字证据快照
    link_source: str = ""  # regex-memo / regex-report / manual
    project_id: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class WorkflowAgent(BaseModel):
    """逐-agent 遥测 — 一个 run 一个 fan-out agent 一行。

    upsert by (wf_id, cc_agent_id)。数据 100% 现成，来自
    `wf_<id>.json.workflowProgress[]` 的 type=workflow_agent 条，无需自聚合。
    """

    id: str = Field(default_factory=_new_id)
    run_id: str  # = workflow_runs.wf_id
    wf_id: str  # 冗余便于直查
    project_id: str = ""  # 隔离
    cc_agent_id: str = ""  # 快照 agentId，与 run_id 组唯一去重键
    os_agent_id: str | None = None  # 链既有成员：agents.cc_tool_use_id == cc_agent_id
    label: str = ""  # 如 map:mcp
    phase_index: int = 0
    phase_title: str = ""
    model: str = ""  # 如 claude-opus-4-8[1m]
    state: str = ""  # queued / running / done
    tokens: int = 0
    tool_calls: int = 0
    duration_ms: int | None = None
    last_tool_name: str = ""
    last_tool_summary: str = ""
    prompt_preview: str = ""
    result_preview: str = ""
    started_at: datetime | None = None
    queued_at: datetime | None = None
    last_activity_at: datetime | None = None  # Phase2: 该 agent jsonl 的 mtime（泳道右端 + 跳过水位）
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class PipelineState(BaseModel):
    """Task 上的 pipeline 运行时状态。存于 task.config['pipeline']。"""

    template: str | None = None
    current_stage: str | None = None
    current_stage_class: str | None = None
    autopilot_active: bool = False
    stage_started_at: datetime | None = None


class StageTransition(BaseModel):
    """Pipeline stage 转换事件。存独立表 pipeline_stage_history（append-only）。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    from_stage: str | None = None
    to_stage: str
    transitioned_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    triggered_by: Literal["manual", "auto", "force", "system"] = "manual"
    reason: str = ""


class ChannelMessage(BaseModel):
    """Channel message — supports cross-team broadcasting with @mention semantics."""

    id: str = Field(default_factory=_new_id)
    channel: str  # "team:<name>" / "project:<id>" / "global"
    sender: str
    content: str
    mentions: list[str] = Field(default_factory=list)  # ["@agent-name", "@team-name"]
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class EcosystemRepoProfile(BaseModel):
    """Claude 生态仓档案 — 广索引检索 + 周期更新。

    项目隔离: project_id=None 表示全局/未归属，每个项目拥有独立的快照行。
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    repo_full_name: str  # "owner/repo"
    name: str
    owner: str
    description: str | None = None
    stars: int = 0
    language: str | None = None
    topics: list[str] = Field(default_factory=list)
    homepage: str | None = None
    last_commit_at: datetime | None = None
    needs_deep_review: bool = False  # True when stars < 15000
    # "agent-framework" / "mcp-server" / "memory-system" / "skill-system" / "tooling"
    relevance_category: str | None = None
    relevance_score: int = 0  # 0-10
    one_line_summary: str | None = None
    last_scanned_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    # Stage B 扩展字段
    pushed_at: datetime | None = None  # GitHub 仓最后 push 时间，用于判活跃度
    is_archived: bool = False  # > 365 天未 push 标记为 deprecated
    scan_run_id: str | None = None  # 关联到扫描批次 EcosystemScanRun.id
    description_excerpt: str = ""  # 描述摘要，用于二次相关性过滤
    # v1.5.0-A 扩展：渐进式漏斗 Stage 0 浅扫 + 失败追踪 + 活跃集
    shallow_summary: str = ""  # Stage 0 agent 浅扫总结（200-400 字，区分 description_excerpt）
    last_shallow_refreshed_at: datetime | None = None  # 上次浅扫刷新时间
    is_deleted: bool = False  # GitHub 端仓被删（API 404）
    is_private_now: bool = False  # GitHub 端仓被设私密（API 403 forbidden, not rate limit）
    last_fetch_error: str = ""  # 最近一次抓取错误的短消息
    fetch_failure_count: int = 0  # 累计失败次数
    is_active: bool = True  # DEPRECATED v1.6.0 P1.A: 请用 last_active_status 代替。此字段仅向后兼容保留。
    active_rank: int | None = None  # 当前项目内排名（按 stars，None=不在 top_n）
    # v1.6.0-P0.4: NormalizedSignal fields (written by index_update)
    canonical_id: str | None = None  # "github/owner/repo" cross-source dedup key
    source_kind: str = "github"  # which data source produced this profile
    last_active_status: str | None = None  # 'active'|'inactive'|'archived'|'manual_archived'
    last_status_change_at: datetime | None = None  # when last_active_status last changed
    popularity_percentile: float | None = None  # 0-1, 1.0 = top of scan results
    activity_score: float | None = None  # 0-1 composite freshness * popularity
    # v1.6.0-P1.A: human-flagged manual status
    manual_status: str | None = None  # 'no_value' | 'pinned' | null
    manual_status_reason: str | None = None
    manual_status_set_at: datetime | None = None
    manual_status_set_by: str | None = None
    # v1.6.0-P1.C-1: JSON array of query strings that discovered this repo
    discovered_via_queries: list[str] = Field(default_factory=list)
    # v1.6.1 multi-source: list of source entries [{kind,id,stars/likes,url,last_seen_at}, ...]
    # 一个 profile 多个来源（GitHub + HF Space + GitLab）合并显示，不再为同项目建多 profile
    sources: list[dict] = Field(default_factory=list)
    # v1.6.1 primary source — decides canonical URL/title; default 'github' for legacy rows
    primary_source: str = "github"


# ============================================================
# Ecosystem 扩展模型 (Stage B)
# ============================================================


class EcosystemDeepReviewStatus(enum.StrEnum):
    """深扫报告状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class IntegrationRecommendation(enum.StrEnum):
    """集成建议级别。"""

    INTEGRATE = "integrate"
    REFERENCE = "reference"
    LEARN = "learn"
    SKIP = "skip"


class DemoResult(enum.StrEnum):
    """Demo 运行结果。"""

    SUCCESS = "success"
    FAIL = "fail"
    SKIPPED = "skipped"


class EcosystemTagCategory(enum.StrEnum):
    """生态标签分类。"""

    CAPABILITY = "capability"
    TECH_STACK = "tech_stack"
    MATURITY = "maturity"
    POSITIONING = "positioning"


class EcosystemTagSource(enum.StrEnum):
    """标签来源。

    v1.5.0-A 新增 LIFECYCLE — 用于漏斗 Stage 3 标记 reference / integrated /
    deleted / private_now / evaluating，由 ecosystem lifecycle 自动写入。
    """

    GITHUB_TOPIC = "github_topic"
    AUTO_RULE = "auto_rule"
    AUTO_LLM = "auto_llm"
    MANUAL = "manual"
    LIFECYCLE = "lifecycle"


class EcosystemStageStatus(enum.StrEnum):
    """生态仓深扫漏斗 stage 状态 (v1.5.0)。

    渐进式累加：queued → shallow_done → architecture_done → debated →
    referenced / integrated。每个 *_failed 子状态表示该阶段重试 5 次仍失败，
    不影响其他 stage 推进。
    """

    QUEUED = "queued"
    SHALLOW_DONE = "shallow_done"
    SHALLOW_FAILED = "shallow_failed"
    ARCHITECTURE_DONE = "architecture_done"
    ARCHITECTURE_FAILED = "architecture_failed"
    DEBATED = "debated"
    DEBATED_FAILED = "debated_failed"
    REFERENCED = "referenced"
    INTEGRATED = "integrated"


# D5 convergence (2026-07): ``stage_status`` is the single authoritative axis
# for deep-review funnel progress; the legacy ``status`` column is demoted to
# a derived read-only view of it. This mapping is the SINGLE SOURCE OF TRUTH —
# the storage choke points (repository.create_deep_review /
# update_deep_review_stage) and the startup backfill
# (repository.backfill_deep_review_dual_axis) all derive from it.
# Do NOT duplicate this mapping in repository / services / routes / frontend.
STAGE_TO_STATUS: dict[EcosystemStageStatus, EcosystemDeepReviewStatus] = {
    EcosystemStageStatus.QUEUED: EcosystemDeepReviewStatus.QUEUED,
    EcosystemStageStatus.SHALLOW_DONE: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.ARCHITECTURE_DONE: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.DEBATED: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.REFERENCED: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.INTEGRATED: EcosystemDeepReviewStatus.COMPLETED,
    EcosystemStageStatus.SHALLOW_FAILED: EcosystemDeepReviewStatus.FAILED,
    EcosystemStageStatus.ARCHITECTURE_FAILED: EcosystemDeepReviewStatus.FAILED,
    EcosystemStageStatus.DEBATED_FAILED: EcosystemDeepReviewStatus.FAILED,
}


def derive_status_from_stage(
    stage: EcosystemStageStatus | str,
) -> EcosystemDeepReviewStatus:
    """Derive the legacy ``status`` view from the authoritative ``stage_status``.

    Accepts either the enum or its string value (normalized first).
    Raises ``ValueError`` for unknown stage strings — same contract as
    ``EcosystemStageStatus(...)``.
    """
    if isinstance(stage, str):
        stage = EcosystemStageStatus(stage)
    return STAGE_TO_STATUS[stage]


class EcosystemRelationType(enum.StrEnum):
    """仓与仓的关联类型。"""

    INSPIRED_BY = "inspired_by"
    FORKS = "forks"
    EXTENDS = "extends"
    COMPETES = "competes"
    DEPENDS_ON = "depends_on"


class EcosystemScanStrategy(enum.StrEnum):
    """扫描策略。"""

    INCREMENTAL = "incremental"
    FULL = "full"
    TOPIC = "topic"
    TRENDING = "trending"


# ============================================================
# v1.6.0 P0: Multi-source data model types
# ============================================================


class DataSourceKind(enum.StrEnum):
    """Supported ecosystem data source kinds."""

    GITHUB = "github"
    HUGGINGFACE = "huggingface"
    NPM = "npm"
    PYPI = "pypi"
    HACKERNEWS = "hackernews"
    PRODUCTHUNT = "producthunt"
    ARXIV = "arxiv"
    CUSTOM = "custom"


class RepoActiveStatus(enum.StrEnum):
    """Active status of a repo in the ecosystem index."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    STALE = "stale"
    ARCHIVED = "archived"


class EcosystemShallowBatch(BaseModel):
    """浅扫批次 — 聚合一次批量浅扫的元信息与候选仓快照。

    状态流转: pending_approval → (approved → running → completed) / cancelled
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    triggered_by: str  # 'cron' / 'manual' / 'user'
    trigger_reason: str | None = None
    candidates_count: int = 0
    candidates_snapshot_json: str | None = None  # JSON list of repo_id
    status: str = "pending_approval"  # pending_approval / approved / running / completed / cancelled
    approved_by: str | None = None
    approved_at: datetime | None = None
    completed_at: datetime | None = None
    new_repos_count: int = 0
    updated_repos_count: int = 0
    metadata_changed_count: int = 0
    failed_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class EcosystemDeepReview(BaseModel):
    """生态仓深扫报告 — 针对单个仓的结构化分析。

    FK 关系：repo_id → EcosystemRepoProfile.id (CASCADE)，report_id → Report.id (可选)。
    项目隔离: project_id=None 表示全局/未归属，深扫报告归属于发起项目。
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    repo_id: str  # FK -> EcosystemRepoProfile.id
    status: EcosystemDeepReviewStatus = EcosystemDeepReviewStatus.QUEUED
    agent_id: str | None = None  # 执行此次深扫的 agent
    summary_md: str = ""
    architecture_md: str = ""
    demo_result: DemoResult | None = None
    demo_log_excerpt: str = ""
    risks_md: str = ""
    learnings_md: str = ""
    integration_recommendation: IntegrationRecommendation | None = None
    report_id: str | None = None  # FK -> Report.id
    dispatch_prompt: str = ""  # sub-agent dispatch prompt (separate from demo_log_excerpt)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    # v1.5.0-A 扩展：渐进式漏斗 stage 状态机 + 关联会议/集成任务
    stage_status: EcosystemStageStatus = EcosystemStageStatus.QUEUED  # 漏斗 stage 状态
    integration_md: str = ""  # Stage 2 详细集成建议（不只是 enum）
    shallow_completed_at: datetime | None = None  # Stage 0 完成时间
    architecture_completed_at: datetime | None = None  # Stage 1 完成时间
    debated_at: datetime | None = None  # Stage 2 辩论结束时间
    stage3_completed_at: datetime | None = None  # Stage 3 referenced/integrated 完成时间
    debate_meeting_id: str | None = None  # FK -> Meeting.id (Stage 2 触发会议)
    integration_task_id: str | None = None  # FK -> Task.id (Stage 3 integrate 派任务)
    # v1.5.3: worker pool claim 字段
    claimed_by: str | None = None  # worker_id 字符串，认领中则非 None
    claimed_at: datetime | None = None  # 认领时间戳
    quality_score: int | None = None  # 0-100 审查质量分
    quality_notes: str | None = None  # 审查理由
    reviewed_by: str | None = None  # 质量审查者 worker_id
    reviewed_at: datetime | None = None  # 质量审查完成时间
    # v1.7.0: 关联浅扫批次
    batch_id: str | None = None  # FK -> EcosystemShallowBatch.id


class EcosystemTag(BaseModel):
    """能力标签字典 — 描述生态仓的能力 / 技术栈 / 成熟度 / 定位。"""

    id: str = Field(default_factory=_new_id)
    name: str  # unique，如 "memory_system"
    aliases: list[str] = Field(default_factory=list)
    category: EcosystemTagCategory
    description: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class EcosystemRepoTag(BaseModel):
    """仓-标签多对多关联。

    FK 关系：repo_id → EcosystemRepoProfile.id (CASCADE)，tag_id → EcosystemTag.id (RESTRICT)。
    Unique constraint: (repo_id, tag_id)。
    项目隔离: project_id 跟随 repo_id 所属项目。
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    repo_id: str  # FK -> EcosystemRepoProfile.id
    tag_id: str  # FK -> EcosystemTag.id
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: EcosystemTagSource = EcosystemTagSource.MANUAL
    agent_id: str | None = None  # 打标人
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class EcosystemRelation(BaseModel):
    """仓与仓的引用 / 衍生关系。

    FK 关系：from_repo_id / to_repo_id → EcosystemRepoProfile.id (CASCADE)。
    项目隔离: 项目内部的研究产出，不跨项目共享。
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    from_repo_id: str  # FK -> EcosystemRepoProfile.id
    to_repo_id: str  # FK -> EcosystemRepoProfile.id
    relation_type: EcosystemRelationType
    evidence: str = ""  # 来源说明
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    agent_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class EcosystemScanRun(BaseModel):
    """扫描批次记录 — 一次扫描任务的执行元数据与统计。

    项目隔离: 扫描历史归属于发起扫描的项目。
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    strategy: EcosystemScanStrategy = EcosystemScanStrategy.INCREMENTAL
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime | None = None
    duration_seconds: float = 0.0
    repos_added: int = 0
    repos_updated: int = 0
    repos_skipped: int = 0
    # v1.6.1 Phase 2: count repos with actual metadata changes (topics/stars/desc/lang)
    metadata_changed_count: int = 0
    errors: list[str] = Field(default_factory=list)
    notes: str = ""
    triggered_by: str = "manual"  # "manual" / "cron"
    agent_id: str | None = None


class EcosystemRepoStatusSnapshot(BaseModel):
    """每次 scan 的仓状态快照 (v1.5.0-A 决策 D — append-only 永不清理)。

    用于追踪 stars 涨跌、push 频率、激活/退出活跃集等历史变化。
    每次 scan 跑完为活跃集中每个仓写一行；用户可通过 UI 看历史 timeline。
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    repo_id: str  # FK -> EcosystemRepoProfile.id
    scan_run_id: str  # FK -> EcosystemScanRun.id (触发的扫描批次)
    snapshot_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    stars: int = 0
    pushed_at: datetime | None = None
    is_archived: bool = False  # GitHub archived 状态
    is_active: bool = True  # 当时是否在项目活跃集
    summary_at_time: str = ""  # 当时的 shallow_summary (供历史比对)


class EcosystemProjectSettings(BaseModel):
    """每个项目的 ecosystem 配置 (v1.5.0-A 决策 C — 项目自定义阈值)。

    项目首次访问 ecosystem 时由系统自动创建默认行；
    AI Team OS 项目使用更严格的默认值 (min_stars=5000, top_n=200)。
    """

    project_id: str  # 主键 — 一项目一行
    min_stars: int = 1000  # 入档阈值
    top_n: int = 200  # 活跃集大小（按 stars 排序前 N）
    refresh_interval_days: int = 7  # 浅扫刷新间隔
    auto_shallow_on_archive: bool = True  # 入档时是否自动跑 Stage 0
    focus_topics: list[str] = Field(default_factory=list)  # 关注 topic 白名单（空=全 topic）
    focus_languages: list[str] = Field(default_factory=list)  # 关注语言白名单（空=全语言）
    # 决策 F：测试驱动调整的并发配置
    shallow_concurrency: int = 5
    deep_concurrency: int = 3
    # v1.6.1 Phase 2: migrated from scan_profile.alert_thresholds.max_new_per_scan
    alert_max_new_per_scan: int = 50
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


# ============================================================
# v1.6.0 P0: Multi-source data model Pydantic types
# ============================================================


class NormalizedSignal(BaseModel):
    """Cross-source normalized activity/popularity signal."""

    popularity_rank: int = 0
    popularity_percentile: float = 0.0  # 0-1, where 0.99 = top 1%
    last_activity_at: datetime | None = None
    activity_score: float = 0.0  # 0-1 composite score


class DataSource(BaseModel):
    """Ecosystem data source configuration (per-project, multi-source)."""

    id: str = Field(default_factory=_new_id)
    project_id: str
    kind: DataSourceKind
    name: str
    config: dict[str, Any] = Field(default_factory=dict)  # queries/filters/rate_limit
    enabled: bool = True
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class ScanProfile(BaseModel):
    """Ecosystem scan profile — versioned config for active/inactive/archive thresholds."""

    id: str = Field(default_factory=_new_id)
    project_id: str
    version: int = 1
    profile: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class EcosystemIndexDiff(BaseModel):
    """Record of a single index_update run's diff output (new/reactivated/deactivated/etc.)."""

    id: str = Field(default_factory=_new_id)
    scan_run_id: str | None = None
    project_id: str | None = None
    diff_type: str = "incremental"  # 'initial' | 'incremental'
    new_count: int = 0
    reactivated_count: int = 0
    deactivated_count: int = 0
    stale_count: int = 0
    archived_count: int = 0  # deprecated: kept for backward compat, use github_archived_changed_count
    # v1.6.0-P1 hotfix: new semantically-correct column names
    github_archived_changed_count: int = 0
    removed_from_query_count: int = 0
    details_json: dict[str, Any] = Field(default_factory=dict)
    markdown_summary: str = ""
    alerted: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class EcosystemStatusChange(BaseModel):
    """Tracks individual repo status transitions (active → inactive, etc.)."""

    id: str = Field(default_factory=_new_id)
    repo_id: str
    project_id: str | None = None
    from_status: str | None = None
    to_status: str
    scan_run_id: str | None = None
    reason: str = ""
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class EcosystemRepoEvent(BaseModel):
    """Full event log for every operation on an ecosystem repo.

    Replaces index_diffs as source-of-truth for change tracking. Diff views
    are computed dynamically by grouping events over a time window.
    """

    id: str = Field(default_factory=_new_id)
    repo_id: str
    project_id: str | None = None
    # 'discovered'|'rescanned'|'topics_changed'|'stars_jumped'|'status_changed'
    # |'archived'|'manual_pinned'|'manual_unpinned'|'removed_from_query'
    event_type: str
    payload_json: dict[str, Any] = Field(default_factory=dict)
    source: str = "scanner"  # 'scanner' | 'manual' | 'api'
    scan_run_id: str | None = None
    # Kept for status_changed compat with EcosystemStatusChange
    from_status: str | None = None
    to_status: str | None = None
    reason: str | None = None
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


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
