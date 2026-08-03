"""AI Team OS — Global shared type definitions.

All modules reference types from this file; they do not define their own data models.
This file is managed by the tech-lead; other engineers only read-reference it.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from aiteam.clock import utc_now

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
    # completed 团队因新成员注册/会话恢复自动复活（hook_translator auto-revive；
    # 2026-07-22 补录：先例 _resolve_cc_team 一直在发此事件但枚举缺席=潜伏 ValueError）
    TEAM_AUTO_REVIVED = "team.auto_revived"
    # 过期会话容器队被 reaper 扫走（与用户手动删队区分开，便于事后追溯是谁删的）
    TEAM_CONTAINER_PURGED = "team.container_purged"

    # Agent events
    AGENT_CREATED = "agent.created"
    AGENT_REMOVED = "agent.removed"
    AGENT_STATUS_CHANGED = "agent.status_changed"

    # Task events
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    # 失败诊断留痕：分析跑过但结果只回给调用方、不进事件流，等于没跑过——
    # 事后既查不到诊断做没做，也无法统计诊断覆盖率。
    TASK_FAILURE_ANALYZED = "task.failure_analyzed"
    TASK_FAILURE_DIAGNOSED = "task.failure_diagnosed"

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
    # CC 自己的队友空闲信号（TeammateIdle hook）。只观察不改状态——CC 的 idle
    # 是"这一轮说完了"，OS 的 offline 是"人没了"，两者不能划等号。
    CC_TEAMMATE_IDLE = "cc.teammate_idle"
    # CC 队友私信（SendMessage）的只读镜像。刻意不写进 channel_messages ——
    # 那张表是 OS 自有广播频道（已规划未启用）的地盘，镜像只进事件流。
    CC_MESSAGE_SENT = "cc.message_sent"
    # 存活判据双轨观察（C13）：transcript mtime 与 CC 会话注册表意见不一致时
    # 落一条，供日后决定是否换主判据。判定本身仍只由 mtime 出。
    SESSION_LIVENESS_DIVERGENCE = "session.liveness_divergence"
    # 压缩检查点：PreCompact 定格 OS 侧作战态，压缩后的 SessionStart 递回。
    SESSION_COMPACT_CHECKPOINT = "session.compact_checkpoint"
    # PostCompact 回执：压缩真的发生了（PreCompact 触发后压缩仍可能取消）。
    SESSION_COMPACT_COMPLETED = "session.compact_completed"
    # 隔离工作区出生/消失（本仓多会话并行纪律要求用 git worktree 隔离，此前 OS 无感）
    CC_WORKTREE_CREATED = "cc.worktree_created"
    CC_WORKTREE_REMOVED = "cc.worktree_removed"
    # CC 原生任务的观测面。桥（cc_task_bridge）只在 TaskCompleted 上记账，而该
    # 事件此前没有并挂遥测，于是"桥触发过几次、滤掉几条"查无实据。这三个事件
    # **只观测不记账**，上墙逻辑仍归桥。
    CC_TASK_CREATED = "cc.task_created"
    CC_TASK_COMPLETED = "cc.task_completed"
    # 中止侧 CC 不给 hook（不存在 TaskStop/TaskAborted 事件，Esc 打断也无声），
    # 只能把 TaskStop **工具调用**从工具事件洪流里拎成一等事件——实测中止才是
    # 主路径（TaskStop 40 次且持续在用，TaskCreate 14 次且早已归零）。
    CC_TASK_STOPPED = "cc.task_stopped"

    # HTTP 请求级账本的小时聚合行（api/request_ledger.py）。给"零调用"判断补第
    # 二个口径：MCP 工具面看不见的调用（Dashboard/hook/脚本/别的会话）都在这里。
    API_REQUEST_ROLLUP = "api.request_rollup"

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
    # 决策现场原文（低频高价值，与被停写的心跳正好相反）：
    # ExitPlanMode 的方案正文 / AskUserQuestion 的问答对
    DECISION_PLAN_PRESENTED = "decision.plan_presented"
    DECISION_USER_ASKED = "decision.user_asked"

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

    # 治理租约易主（A2-obs，辩论 503e07f1 议题A）：前任 holder 还挂在行上、租约已过期，
    # 被另一个实例抢走。只在这一条分支上发——续约、无主认领、主动让出后接手都不算交替。
    # 目的是先回答"这事到底发生过没有"：观测期内一条都没有，A2-impl（epoch 列）就不必做。
    GOVERNANCE_LEASE_TAKEN_OVER = "governance.lease_taken_over"


# ============================================================
# Token 口径（token 用量归因 v1，阶段 0「口径正名」）
# ============================================================
# 一个 token 数值脱离口径就没有意义。本库同时存在两个正交的 token 口径，实测相差
# 5~25 倍（同一批 agent：117,800 vs 632,504 … 127,089 vs 3,236,088）。把它们相加、
# 并列、或塞进同一个"总 token"，就是本仓刚在时间戳上栽过的同类事故——混口径。
# 规格见 docs/token-attribution-v1-design.md §0.2 / §1.1。


class TokenMetric(enum.StrEnum):
    """token 数值的口径 —— 归因层的一等维度，与覆盖率并列。

    只有这两个成员，因为只有这两个口径在回答"用量"问题。取值语义：

    * ``USAGE_SUM``：一份 transcript 内按 ``requestId`` 分组、每组取最后一条 usage
      快照、再跨组累加的四层 token。回答"这段工作一共用掉多少"。
    * ``CTX_LAST``：最后一条 assistant 消息的四字段和，是一个**瞬时上下文水位快照**，
      不是消耗量。回答"这个 agent 结束时上下文有多满"。

    两者**永不相加**。
    """

    USAGE_SUM = "usage_sum"
    CTX_LAST = "ctx_last"


# 第三个已存在的 token 口径：上下文水位（``agents.ctx_tokens`` / ``ctx_pct``，由
# ``agent_context.measure`` 产出，服务于 agent 复用治理）。它**刻意不是 TokenMetric
# 成员**——TokenMetric 覆盖的是"用量归因"这一个问题域，而水位口径与用量无关（§1.1）。
# 但它仍需一个可标注的名字：呈现面上任何 token 数值都必须挂口径，包括不参与归因的。
# 三个口径互不相加。
CTX_WATERMARK_METRIC = "ctx_watermark"

# 全部合法口径标签 —— 呈现面标注与机检（I13）共用的封闭集合。
TOKEN_METRIC_LABELS: frozenset[str] = frozenset(
    {TokenMetric.USAGE_SUM.value, TokenMetric.CTX_LAST.value, CTX_WATERMARK_METRIC}
)

# 口径 → (中文短名, 产出者, 一句话定义)。呈现面的口径徽标与页脚说明取自这里，
# 避免同一段解释在页面、文档、注释里各写一版然后各自漂移。
TOKEN_METRIC_SPECS: dict[str, tuple[str, str, str]] = {
    TokenMetric.USAGE_SUM.value: (
        "用量累加",
        "services/token_attribution.parse_transcript_usage → agents 五列",
        "按 requestId 分组取末条快照后跨组累加的四层 token；回答一共用掉多少。",
    ),
    TokenMetric.CTX_LAST.value: (
        "末轮上下文水位",
        "api/workflow_ingest._last_assistant_ctx_tokens → workflow_agents.tokens",
        "最后一条 assistant 消息的四字段和；是瞬时水位快照，不是消耗量。",
    ),
    CTX_WATERMARK_METRIC: (
        "上下文水位（复用治理）",
        "api/agent_context.measure → agents.ctx_tokens / ctx_pct",
        "agent 当前占了多少上下文；服务于复用决策，与用量归因无关。",
    ),
}

# token 用量的四层 —— "总量"实测 95.6% 是 cache_read，只报总量等于只报缓存读取量，
# 跨模型/跨派工路径的比较会被系统性带偏。因此四层必须始终可分列（§1.2）。
TOKEN_LAYERS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)


class TokenSource(enum.StrEnum):
    """``agents.tokens_source`` 的取值 —— 这一行的 token 数是怎么来的。

    列为 NULL 表示**尚未采集**，既不是 transcript 定真也不是别名兜底——no-data 与
    zero 必须分得开（§2.6）。
    """

    TRANSCRIPT = "transcript"  # 从 transcript 逐行解析定真
    ALIAS_FALLBACK = "alias_fallback"  # transcript 已灭失，读侧按别名台账兜底推得


class AttributionScope(enum.StrEnum):
    """一次归因查询问的是"哪一段工作"。

    前四档沿归因链自上而下（§2.1），每一档的分母都是**该档内的派工数**，不是
    "该档内已测到的派工数"——后者会让覆盖率恒等于 100%（§4.1）。

    ``TASK`` 是旁支而非第五级：task 边靠寄生在记账行为上采得（§2.4），实测覆盖率
    接近 0，与前四档不是一个量级的可信度，任何呈现都必须单独标注。
    """

    PROJECT = "project"
    SESSION = "session"
    WORKFLOW_RUN = "workflow_run"
    AGENT = "agent"
    TASK = "task"


class AttributionMethod(enum.StrEnum):
    """这一次归因的数是**怎么来的** —— 与 ``TokenSource`` 是行级/查询级的关系。

    ``TokenSource`` 记的是"agents 某一行的四层数从哪来"，随行持久化；本枚举记的是
    "这一次聚合查询整体走的是哪条路"，随查询结果返回。二者同名不同层，别混用。
    """

    TRANSCRIPT_PARSE = "transcript_parse"  # 由 transcript 逐行解析定真（usage_sum 正路）
    SELF_REPORT = "self_report"  # 采自 workflow JSON / journal 的自报值（ctx_last 正路）
    ALIAS_FALLBACK = "alias_fallback"  # transcript 已灭失，读侧按别名台账兜底


class UnattributedReason(enum.StrEnum):
    """未归因不是一种状态，是几种，处置方式各不相同（§3.4）。

    这张表存在的唯一理由是**让"覆盖率 78%"变成一句可行动的话**——看的人应当据此
    知道剩下 22% 能不能救。所以"救不回来"与"还没去救"必须分开标，把前者并进后者
    会让这个枚举失去全部价值。

    前四个是 §3.4 的原表。后两个是具名扩展，因为 §3.4 那张表是按 usage_sum 的子
    agent 路径写的，硬套到另外两条路径上就会说谎：

    * ``SELF_REPORT_ABSENT`` —— ctx_last 侧的 ``workflow_agents.tokens`` 为 0 时，
      原因是那次 run 的 JSON 从头就没带遥测（数据源只有请求规格）。它不是
      ``NOT_YET_MEASURED``：没有任何"再跑一次采集"能把它补回来。
    * ``MULTI_TASK_UNSPLITTABLE`` —— agent 在多个 task 上都留过账时，它的四层数是
      **整个生命周期的一个合计**，没有逐区间用量可供按边的时间序切分。§2.4 定死
      这种情况如实计未归因，**禁止平均分摊**（分摊会造出无法证伪的数字）。

    ``BY_DESIGN`` 是 §3.4 的第四码，但它描述的是**工具调用级**（``agent_activities``
    52,119 条无 token 字段，是主动选择不采）。工具调用与派工不是同一个单位，所以它
    只作呈现面上的一行标签，**绝不进** :attr:`TokenAttribution.unattributed_reasons`
    ——那个 dict 与 ``dispatches_total`` 同单位，混进活动数就是混量纲。
    """

    NO_TRANSCRIPT_PATH = "no_transcript_path"  # 行从未登记 transcript 路径（历史行，救不回）
    TRANSCRIPT_GONE = "transcript_gone"  # 路径有但文件已不在（随时间只增不减，救不回）
    NOT_YET_MEASURED = "not_yet_measured"  # 路径在、文件在，只是还没跑过采集（可救）
    BY_DESIGN = "by_design"  # 设计上不采集（工具调用级）——见类文档，不进 dict
    SELF_REPORT_ABSENT = "self_report_absent"  # ctx_last 侧自报值缺失（救不回）
    MULTI_TASK_UNSPLITTABLE = "multi_task_unsplittable"  # task 级切不开，如实计未归因


class TokenAttribution(BaseModel):
    """一次归因查询的完整答案 —— 数值与其分母、口径同生共死。

    没有 ``total`` 这个字段，也没有任何方法返回它。调用方要渲染数值，就必须同时
    拿到 ``dispatches_total`` 与 ``metric`` —— 分母和口径是数据的一部分，不是可选
    的装饰。这条**必须落在类型层**：页面标注是软约束，三个月后的自己会忽略它
    （§2.5，此结构由 I13 机检钉住）。

    **为什么没有合计字段**：实测四层里 ``cache_read`` 占 95.6%，任何"总量"实际上
    是"缓存读取量"的同义词；只报总量会让跨模型、跨派工路径的比较被系统性带偏
    （§1.2）。四层必须始终可分列，合计要算由调用方自己负责并自己承担解释责任。

    **口径（metric）无默认值**是刻意的：不标注口径就构造不出对象。本库同时存在
    ``usage_sum``（用量累加）与 ``ctx_last``（末轮上下文水位）两个正交口径，实测
    差 5~25 倍；把它们相加或并列，就是本仓刚在时间戳上栽过的同类事故（§0.2 / R1）。
    本结构两种口径都能承载，**判断依据永远是 metric 字段本身**，不是字段名。

    不变量（有契约测试钉住，见 tests/unit/test_token_attribution_contract.py）：

    * 不存在 ``total`` / ``total_tokens`` 字段或属性；
    * ``dispatches_attributed`` + Σ``unattributed_reasons`` == ``dispatches_total``；
    * ``unattributed_reasons`` 的单位是**派工数**，与分母同单位。
    """

    model_config = {"frozen": True}

    scope: AttributionScope  # project / session / workflow_run / agent / task
    scope_id: str
    metric: TokenMetric  # usage_sum | ctx_last —— 强制标注，无默认值
    input_tokens: int  # 四层分列，不提供合计字段
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    dispatches_attributed: int  # 分子：本 scope 内已测到用量的派工数
    dispatches_total: int  # 分母：本 scope 内的派工总数
    unattributed_reasons: dict[str, int]  # 未归因派工按原因分类计数（§3.4）
    measured_window: tuple[datetime, datetime] | None
    method: AttributionMethod  # transcript_parse | self_report | alias_fallback


class DispatchPopulation(enum.StrEnum):
    """派工路径 —— 覆盖率矩阵的行（§5.2），也是聚合查询的必填维度之一。

    为什么它必须是查询维度而不是事后分组：三条路径的量级差着数量级（实测单个
    Leader 会话 8.5 亿 token，远超全部子 agent 之和），混进一个排行榜里子 agent
    的归因结果会被彻底淹没。§3.3 因此定死"主会话与子 agent 必须分列呈现，且默认
    不合并" —— 把它做成必填参数，合并就得先显式发起两次查询再自己动手加，加的人
    要自己承担这个动作。
    """

    SUBAGENT = "subagent"  # agents 表 role != 'leader' —— 派工（usage_sum）
    LEADER_SESSION = "leader_session"  # agents 表 role == 'leader' —— 主会话（usage_sum）
    WORKFLOW_SELF_REPORT = "workflow_self_report"  # workflow_agents 自报（ctx_last，仅覆盖率）
    TOOL_CALL = "tool_call"  # agent_activities 工具调用级 —— 设计上不采集（§3.2）


class UsageCoverageRow(BaseModel):
    """覆盖率矩阵的一行 —— 分子、分母、口径，**刻意零 token 数值字段**。

    §4.3 与 §5.2 那两张表本来就是覆盖率表：列是"派工数 / 已测量 / 覆盖率 / 口径"，
    没有一列是 token 数。这一点让 ``ctx_last`` 路径也能如实上表——它的四层分解从
    未被保存过（``workflow_agents.tokens`` 在 ingest 时已是四字段之和），进不了
    :class:`TokenAttribution`，但它的分子分母是实打实的。

    ``dispatches_total`` / ``dispatches_attributed`` 可为 None，专给"设计上不采集"
    那一行用：**空白会被读成 bug，0 会被读成"真的一次都没有"**，而事实是这个问题
    在这条路径上不适用（§5.2：「设计上不采集」是一个正式取值，不是空白）。
    """

    path: DispatchPopulation
    metric: str  # TokenMetric 取值 或 ""（该路径不产出用量数值）
    dispatches_total: int | None
    dispatches_attributed: int | None
    unattributed_reasons: dict[str, int] = Field(default_factory=dict)
    note: str = ""


class EdgeCoverage(BaseModel):
    """归因链上一跳的可解析率 C_hop（§4.1）。

    端到端覆盖率是各跳的乘积，用一个标量表达会掩盖真正的瓶颈——实测瓶颈不在
    token 采集而在 ``agent→task`` 这一跳（0.8%），只看总覆盖率永远看不出来。
    """

    edge: str  # 如 "agent->session"
    resolvable: int  # 分子：该边可解析的行数
    required: int  # 分母：需要该边的行数
    note: str = ""


class UsageCoverageReport(BaseModel):
    """覆盖率向量的完整答案 —— 页面第一屏第一块的数据源（§5.2 ①②）。

    刻意**不含任何 token 数值**：本结构回答"测到了多少比例"，token 值一律走
    :class:`TokenAttribution`（那里每个数字都自带分母）。因此它不进
    ``scripts/usage_surface.py`` 的 PY_SURFACES —— 那张注册表管的是 token 呈现面，
    而这里一个 token 字段都没有。

    ``rows`` 刻意不提供合计行：``usage_sum`` 与 ``ctx_last`` 两个口径实测差 5~25 倍，
    任何跨行合计都是混口径（§0.2 / §5.2）。
    """

    rows: list[UsageCoverageRow]
    hops: list[EdgeCoverage]
    window: tuple[datetime, datetime] | None  # 按 created_at 落窗，不是 tokens_measured_at
    generated_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Phase(BaseModel):
    """Phase data model — execution phase under a Project."""

    id: str = Field(default_factory=_new_id)
    project_id: str
    name: str
    description: str = ""
    status: PhaseStatus = PhaseStatus.PLANNING
    order: int = 0
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    # 拥有此容器队的 CC 进程。**派生字段，不落库**：由 API 在响应时解析，供展示层
    # 把同一进程的历史 + 当前容器队合成一组。证不出来就是 None（绝不猜），非容器
    # 队恒为 None。
    cc_pid: int | None = None


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
    created_at: datetime = Field(default_factory=utc_now)
    last_active_at: datetime | None = None
    # Agent reuse governance P1 (batch 1B): sub-agent context watermark ledger.
    # Populated from the sub-agent transcript on SubagentStop + reaper backfill;
    # reuse_domain is provisioned for the P2 decision layer (not written in P1).
    # See docs/agent-reuse-design.md section 4.
    # 口径: CTX_WATERMARK_METRIC（上下文水位，不参与用量归因，与下面四层不相加）
    ctx_tokens: int | None = None  # last measured context token total (D1 formula)
    ctx_window: int | None = None  # detected window size (e.g. 1_000_000)
    ctx_pct: float | None = None  # ctx_tokens / ctx_window * 100
    transcript_path: str | None = None  # sub-agent transcript pointer (resume/re-read anchor)
    ctx_measured_at: datetime | None = None  # when the watermark was last measured
    reuse_domain: str | None = None  # most-recent task domain tag (P2 decision layer)
    # 口径: TokenMetric.USAGE_SUM（用量累加，四层分列）。与上面的 ctx_* 水位是两回事，
    # 两者不相加。None = 尚未采集到，不等于 0 —— no-data 与 zero 必须分得开。
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    tokens_measured_at: datetime | None = None
    # 这一行的四层数是怎么来的：transcript 定真 / 别名兜底。None = 未采集。
    # 只做审计溯源，不参与任何计算（§2.6 本设计新增的唯一一列）。
    tokens_source: TokenSource | None = None


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
    # CC 原生任务的 id（TaskCompleted 载荷的 task_id）。只有由 cc_task_bridge
    # 镜像进来的行才有值，是镜像的幂等键——同一个 CC 任务重复完成不会建第二行。
    cc_task_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


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
    created_at: datetime = Field(default_factory=utc_now)
    accessed_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)


class Event(BaseModel):
    """System event data model."""

    id: str = Field(default_factory=_new_id)
    type: EventType
    source: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)
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
    created_at: datetime = Field(default_factory=utc_now)
    concluded_at: datetime | None = None


class MeetingMessage(BaseModel):
    """Meeting message data model."""

    id: str = Field(default_factory=_new_id)
    meeting_id: str
    agent_id: str
    agent_name: str
    content: str
    round_number: int = 1
    timestamp: datetime = Field(default_factory=utc_now)
    msg_metadata: dict[str, Any] = Field(default_factory=dict)  # audit: impersonation, actual_author, etc.


class AgentActivity(BaseModel):
    """Agent activity record — logs each agent tool call."""

    id: str = Field(default_factory=_new_id)
    agent_id: str
    session_id: str
    tool_name: str  # Tool name (Bash, Edit, Read, Agent, etc.)
    input_summary: str = ""  # Input summary (e.g. command, file path)
    output_summary: str = ""  # Output summary (truncated to 500 chars)
    timestamp: datetime = Field(default_factory=utc_now)
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
    created_at: datetime = Field(default_factory=utc_now)
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
    next_run_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)


class WakeSession(BaseModel):
    """Record of a single wake_agent subprocess execution."""

    id: str = Field(default_factory=_new_id)
    scheduled_task_id: str
    agent_name: str
    team_id: str = ""
    started_at: datetime = Field(default_factory=utc_now)
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
    tags: list[str] = Field(default_factory=list)  # free-form, for filtering the queue
    created_at: datetime = Field(default_factory=utc_now)
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
    created_at: datetime = Field(default_factory=utc_now)


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
    # 口径: TokenMetric.CTX_LAST —— 快照 totalTokens = Σ 各 agent 的末轮上下文水位，
    # **不是**这次 run 一共烧了多少。与 agents 五列（USAGE_SUM）实测差 5~25 倍，
    # 永不相加。本阶段只正名，数据与算法一概不动（§5.3 / §6.4-1）。
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
    # 口径: TokenMetric.CTX_LAST（同 total_tokens，只是运行期的那一版估值）
    live_tokens: int | None = None  # 运行期估值 = Σ agents lastCtx（cached 记 0）；终态 UI 用 total_tokens
    last_activity_at: datetime | None = None  # max(journal+agent jsonl mtime)；单调取 max
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)


# ``agent --worked_on--> task`` 边的词汇（§2.4）。写侧寄生在 task_memo_add /
# task_update / report_save 三个**已经必然发生**的记账动作里，读侧在归因聚合里按它
# 筛选。两侧必须共用同一份字面量：写错一个字，边照样写进去、查询照样返回空，
# 而且不会有任何报错 —— 只会表现为"task 级归因覆盖率永远是 0"，一个看起来像是
# 数据问题的代码问题。
AGENT_TASK_LINK_FROM_KIND = "agent"
AGENT_TASK_LINK_TO_KIND = "task"
AGENT_TASK_LINK_TYPE = "worked_on"
# 这条边的语义边界（写进代码是因为它极易被读成另一个意思）：它说的是"这个 agent
# 在这个 task 上留过账"，**不是**"这个 agent 的全部 token 都属于这个 task"。
AGENT_TASK_LINK_SOURCE = "ledger-parasite"


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
    # 口径: TokenMetric.CTX_LAST —— 末轮上下文水位快照，不是这个 agent 的消耗量。
    # 同一 agent 的 USAGE_SUM 走 os_agent_id 关联到 agents 五列去取，**绝不覆写本列**
    # （§6.4-1：用关联解决，不用覆写解决）。0 在本列兼表"未采到"，是 ctx_last 侧的
    # 历史遗产，本阶段不动数据也不动算法，由 I13 记在案（§5.3）。
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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class StageTransition(BaseModel):
    """Pipeline stage 转换事件。存独立表 pipeline_stage_history（append-only）。

    RETAINED (pipeline subsystem retired 2026-07): the pipeline runtime is gone and
    nothing writes this table any more, but the table itself is append-only history
    that must stay readable, so this row schema backs PipelineStageHistoryModel.
    Do not delete — deleting it would break the retained ORM model.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    from_stage: str | None = None
    to_stage: str
    transitioned_at: datetime = Field(default_factory=utc_now)
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
    created_at: datetime = Field(default_factory=utc_now)


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
    last_scanned_at: datetime = Field(default_factory=utc_now)
    first_seen_at: datetime = Field(default_factory=utc_now)
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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)
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
    created_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)


class EcosystemScanRun(BaseModel):
    """扫描批次记录 — 一次扫描任务的执行元数据与统计。

    项目隔离: 扫描历史归属于发起扫描的项目。
    """

    id: str = Field(default_factory=_new_id)
    project_id: str | None = None
    strategy: EcosystemScanStrategy = EcosystemScanStrategy.INCREMENTAL
    started_at: datetime = Field(default_factory=utc_now)
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
    snapshot_at: datetime = Field(default_factory=utc_now)
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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ScanProfile(BaseModel):
    """Ecosystem scan profile — versioned config for active/inactive/archive thresholds."""

    id: str = Field(default_factory=_new_id)
    project_id: str
    version: int = 1
    profile: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)


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
    generated_at: datetime = Field(default_factory=utc_now)


class EcosystemStatusChange(BaseModel):
    """Tracks individual repo status transitions (active → inactive, etc.)."""

    id: str = Field(default_factory=_new_id)
    repo_id: str
    project_id: str | None = None
    from_status: str | None = None
    to_status: str
    scan_run_id: str | None = None
    reason: str = ""
    triggered_at: datetime = Field(default_factory=utc_now)


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
    triggered_at: datetime = Field(default_factory=utc_now)


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
