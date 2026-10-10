"""用量归因行级标记的判定规则 —— 唯一出处。

用户于 2026-09-30 裁定「用量归因页全部显示真实修正后的数据」：不是真实派工/会话的
行不删，打标后所有统计一律排除；历史上没标 harness 的行，认得出来源的补标。这里
只有**判定**：输入是行的只读快照与几个文件探针，输出是"该打什么标、凭什么"。写库
由 ``aiteam attribution repair``（``services.attribution_repair``，默认 dry-run、带
journal）做，统计侧只读标记列、不重新判定——判定依赖文件是否还在，而文件会被宿主
定期清掉；结论在打标那一刻冻结下来，统计才不会随清理悄悄变化。

规则取证见归因梳理报告 c20b37a8（R1、R2、P-1、P-2、P-4）。每条规则只收证据确凿的
行，认不准的一律不标：把说不清的行移出分母，覆盖率就被悄悄做好看了。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiteam.services.usage_coverage import LEADER_ROLE, WORKFLOW_SUBAGENT_ROLE, is_in_flight
from aiteam.types import AgentStatus, AttributionExclusion, HarnessId, UnattributedReason

# 空壳判定前的等待：刚启动、还没来得及调工具或落 transcript 的真会话，在这段时间里
# 与空壳同形。30 分钟后仍然零工具调用、没有 transcript，才是空壳。
RESUME_SHELL_GRACE = timedelta(minutes=30)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-([0-9a-f])[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")
# CC 的子 agent id：a + 16 位十六进制（agent-<id>.jsonl 的文件名中缀）。
_CC_AGENT_ID_RE = re.compile(r"^a[0-9a-f]{16}$")


def uuid_version(value: str | None) -> str:
    """UUID 字符串的版本位；不是 UUID 返回空串。"""
    match = _UUID_RE.match((value or "").lower())
    return match.group(1) if match else ""


@dataclass(frozen=True)
class AgentSnapshot:
    """判定需要的一行 agents 字段（只读快照）。"""

    id: str
    role: str
    source: str
    status: str
    harness: str | None
    session_id: str | None
    cc_tool_use_id: str | None
    transcript_path: str | None
    tokens_measured_at: datetime | None
    created_at: datetime | None
    team_name: str = ""
    activity_count: int = 0
    last_active_at: datetime | None = None


@dataclass(frozen=True)
class ExclusionMark:
    agent_id: str
    kind: AttributionExclusion
    of: str | None
    evidence: str


@dataclass(frozen=True)
class UnmeasurableMark:
    agent_id: str
    reason: UnattributedReason
    evidence: str


@dataclass(frozen=True)
class HarnessMark:
    agent_id: str
    harness: HarnessId
    evidence: str


def _representative_key(row: AgentSnapshot) -> tuple:
    """同一份 transcript 的几行里谁当代表：键越大越优先。

    已测量的行优先（账已经挂在它上面）；其中有 session_id 的优先（幽灵行的
    session_id 在历史上被抹掉了，而它们身上的数是 08-03 回采时的中途快照）；再比
    测量时刻，越新越接近 transcript 当前的累计值；最后比建行时间。**不能取最早的
    行**：最早的正是那批幽灵行。
    """
    def latest(value: datetime | None) -> tuple[bool, datetime | None]:
        return (value is not None, value)

    return (
        row.tokens_measured_at is not None,
        bool(row.session_id),
        latest(row.tokens_measured_at),
        latest(row.created_at),
        row.id,
    )


def identify_exclusions(
    rows: Iterable[AgentSnapshot],
    *,
    session_transcript_exists: Callable[[str], bool],
    now: datetime,
) -> list[ExclusionMark]:
    """该排除出用量统计的 Leader 行。

    ``session_transcript_exists(session_id)``：宿主的会话目录里有没有这个会话 id 的
    transcript（CC 是 ``~/.claude/projects/*/<session_id>.jsonl``）。

    两条规则，互不重叠（空壳没有 transcript 路径，重复行都有）：

    * **重复行**：几个 Leader 行登记的是同一份 transcript。transcript 是累计文件，
      一份只能计一次，由 :func:`_representative_key` 选出的代表行计，其余排除。
    * **resume 空壳**：CC Leader 行，有会话 id，却没有 transcript 路径、没测过用量、
      一次工具调用都没有，已离线且过了 :data:`RESUME_SHELL_GRACE`，会话目录里也没有
      这个会话 id 的 transcript。任何一条不满足就不算——宁可留在分母里算未归因。
    """
    leaders = [row for row in rows if row.role == LEADER_ROLE]
    marks: list[ExclusionMark] = []

    by_transcript: dict[str, list[AgentSnapshot]] = defaultdict(list)
    for row in leaders:
        if row.transcript_path:
            by_transcript[row.transcript_path].append(row)
    for path, group in by_transcript.items():
        if len(group) < 2:
            continue
        representative = max(group, key=_representative_key)
        for row in group:
            if row.id != representative.id:
                marks.append(
                    ExclusionMark(
                        row.id,
                        AttributionExclusion.DUPLICATE_TRANSCRIPT,
                        representative.id,
                        f"与 {len(group) - 1} 行共用 {path}，由 {representative.id} 代表",
                    )
                )

    for row in leaders:
        if row.harness not in (None, HarnessId.CLAUDE_CODE):
            continue
        if not row.session_id or row.transcript_path or row.tokens_measured_at is not None:
            continue
        if row.activity_count or row.status != AgentStatus.OFFLINE:
            continue
        if row.created_at is None or now - row.created_at < RESUME_SHELL_GRACE:
            continue
        if session_transcript_exists(row.session_id):
            continue
        marks.append(
            ExclusionMark(
                row.id,
                AttributionExclusion.RESUME_SHELL,
                None,
                f"会话 {row.session_id} 没有 transcript、零工具调用、已离线",
            )
        )
    return marks


def identify_unmeasurable(
    rows: Iterable[AgentSnapshot],
    *,
    reason_of: Callable[[str], str | None],
    transcript_exists: Callable[[str], bool],
    now: datetime,
    codex_reason_of: Callable[[AgentSnapshot], str | None] | None = None,
) -> list[UnmeasurableMark]:
    """该记下"没有可计用量"结论的行（``usage_unmeasurable``）。

    ``reason_of(path)`` 是 ``token_attribution.unmeasurable_reason``：只有合成错误行得
    ``api_error_only``，一条 assistant 行都没有得 ``no_usage_lines``，其余 None。
    ``codex_reason_of(row)`` 对 Codex 行做同样的判断：解析器找到了这个线程的 rollout、
    里面却没有任何用量事件时得 ``no_usage_lines``；找不到或拒测时 None（找不到说明不了
    用量）。不给就不碰 Codex 行。

    只看已经结束的行：未测量，而且**不在飞**（:func:`usage_coverage.is_in_flight`）。
    在飞的行首个回复可能还没落盘，此刻看文件得出的结论会在它跑完之前一直挂在行上
    （收尾事件丢了就没人清），所以这一刻不下结论。CC 行还要有 transcript 路径且文件在；
    Codex 行按线程 id 找原件，不要求登记路径。
    """
    marks: list[UnmeasurableMark] = []
    for row in rows:
        if row.tokens_measured_at is not None or is_in_flight(row.status, row.last_active_at, now):
            continue
        if row.harness == HarnessId.CODEX:
            if codex_reason_of is None:
                continue
            reason = codex_reason_of(row)
            thread = row.session_id if row.role == LEADER_ROLE else row.cc_tool_use_id
            evidence = f"rollout of thread {thread} has no usage event"
        else:
            if not row.transcript_path or not transcript_exists(row.transcript_path):
                continue
            reason = reason_of(row.transcript_path)
            evidence = f"no billed usage line in {row.transcript_path}"
        if reason:
            marks.append(UnmeasurableMark(row.id, UnattributedReason(reason), evidence))
    return marks


def infer_harness(
    row: AgentSnapshot,
    *,
    cc_session_transcript_exists: Callable[[str], bool],
    codex_rollout_exists: Callable[[str], bool],
) -> HarnessMark | None:
    """一行未标注 harness 的历史行认得出来源吗。认不出返回 None，行保持未标注。

    证据按强弱排，命中第一条即止，证据名随标记进 journal：

    1. transcript 路径在 ``/.claude/projects/`` 下 → claude-code；在 ``/.codex/`` 下 → codex；
    2. ``workflow-subagent`` 行、或所在队是 ``workflow-`` 队 → claude-code（Workflow 只有 CC 有）；
    3. 会话 id 与派工 id 同为 UUIDv7 → codex（Codex 的线程 id 是 v7；两列同查，单列
       v7 实测误命中过 291 行）；
    4. 会话目录里有这个会话 id 的 CC transcript → claude-code；有以它命名的 Codex
       rollout → codex；
    5. 派工 id 是 CC 的子 agent id 形态（a + 16 位十六进制）→ claude-code；
    6. hook 建的行、会话 id 是 UUIDv4 → claude-code（CC 的会话 id 是 v4；09-30 全库
       5,548 行 CC transcript 路径的行没有一行是 v7，67 行 Codex 行没有一行是 v4）。
    """
    if row.harness is not None:
        return None
    path = row.transcript_path or ""
    if "/.claude/projects/" in path:
        return HarnessMark(row.id, HarnessId.CLAUDE_CODE, "cc_transcript_path")
    if "/.codex/" in path:
        return HarnessMark(row.id, HarnessId.CODEX, "codex_rollout_path")
    if row.role == WORKFLOW_SUBAGENT_ROLE:
        return HarnessMark(row.id, HarnessId.CLAUDE_CODE, "workflow_subagent")
    if row.team_name.startswith("workflow-"):
        return HarnessMark(row.id, HarnessId.CLAUDE_CODE, "workflow_team")
    session_version = uuid_version(row.session_id)
    if session_version == "7" and uuid_version(row.cc_tool_use_id) == "7":
        return HarnessMark(row.id, HarnessId.CODEX, "dual_uuid7_ids")
    if row.session_id:
        if cc_session_transcript_exists(row.session_id):
            return HarnessMark(row.id, HarnessId.CLAUDE_CODE, "cc_session_transcript")
        if codex_rollout_exists(row.session_id):
            return HarnessMark(row.id, HarnessId.CODEX, "codex_rollout_by_session")
    if _CC_AGENT_ID_RE.match(row.cc_tool_use_id or ""):
        return HarnessMark(row.id, HarnessId.CLAUDE_CODE, "cc_agent_id_format")
    if row.source == "hook" and session_version == "4":
        return HarnessMark(row.id, HarnessId.CLAUDE_CODE, "session_id_uuid4")
    return None
