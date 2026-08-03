"""列表类工具返回体的精简投影视图（表示层，不动 API 路由与数据层）。

设计规格见 docs/tool-loading-design.md §4。要点（基准任务 4267426d 实测背书）：

- 默认 ``fields="compact"`` 返回精简投影：task-wall 省 84.3% / events 省
  50.7% / ecosystem 列表省 80.7% token；``fields="all"`` 为逃生舱返回全量。
- 返回体携带 ``view`` + ``hint`` 自标识——让 agent 明确知道这是精简视图
  而非字段缺失，并指路单体详情工具（用户裁定 2026-07-14）。
- 投影三铁律：后续调用要用的键（id）永远完整；语义内容只降级为截断摘要、
  不删除；选择动作用得上的信号字段（score/assigned_to/depends_on 等）保留。

名册类工具（agent_list / team_status / team_briefing）多一条正交规则（2026-08-03
成员名册打爆事故后立）：``fields`` 只管**行有多宽**，``include_offline`` 只管
**收哪些行**——``fields="all"`` 不等于"不过滤"。理由是实测那支 51 人会话队里
offline 行占 payload 96.4%：若 ``fields="all"`` 顺带把 offline 全带回来，逃生舱
自己就超上限，等于没有逃生舱。两个开关一起给才是今天的原始返回。
"""

from __future__ import annotations

import json
from typing import Any

# ------------------------------------------------------------------
# fields 参数解析
# ------------------------------------------------------------------

_COMPACT_VALUES = ("", "compact")
_FULL_VALUES = ("all", "full")

FIELDS_ERROR = 'fields 仅支持 "compact"（默认精简视图）或 "all"（全字段）'


def resolve_view(fields: str) -> str | None:
    """把 fields 参数归一化为 "compact" / "all"；无法识别返回 None。"""
    value = (fields or "").strip().lower()
    if value in _COMPACT_VALUES:
        return "compact"
    if value in _FULL_VALUES:
        return "all"
    return None


# ------------------------------------------------------------------
# 通用截断
# ------------------------------------------------------------------


def excerpt(text: str | None, max_chars: int) -> str:
    """截断长文本为摘要（保语义、不删除的降级手段）。"""
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


# ------------------------------------------------------------------
# 行级投影（白名单按"扫列表做选择"这个动作设计）
# ------------------------------------------------------------------


def compact_task_row(task: dict[str, Any]) -> dict[str, Any]:
    """任务墙行投影：挑任务所需信号全保留，描述/结果降级为 80 字摘要。"""
    row: dict[str, Any] = {
        "id": task.get("id"),
        "title": task.get("title"),
        "priority": task.get("priority"),
        "status": task.get("status"),
        "score": task.get("score"),
        "assigned_to": task.get("assigned_to"),
        "tags": task.get("tags") or [],
        "desc": excerpt(task.get("description"), 80),
    }
    # 选择相关的稀疏信号：有值才带，保持默认行体量最小
    if task.get("result"):
        row["result"] = excerpt(task.get("result"), 80)
    if task.get("depends_on"):
        row["depends_on"] = task["depends_on"]
    if task.get("subtasks"):
        row["subtask_count"] = len(task["subtasks"])
    return row


_EVENT_SUMMARY_KEYS = (
    "intent_summary",
    "tool_input_summary",
    "message",
    "summary",
    "title",
    "reason",
)


def compact_event_row(event: dict[str, Any]) -> dict[str, Any]:
    """事件行投影：payload 坍缩成一行派生摘要（非删除），恒空字段不输出。"""
    data = event.get("data") or {}
    summary = ""
    for key in _EVENT_SUMMARY_KEYS:
        value = data.get(key)
        if value:
            summary = str(value)
            break
    if not summary and data:
        summary = json.dumps(data, ensure_ascii=False)
    return {
        "id": event.get("id"),
        "type": event.get("type"),
        "source": event.get("source"),
        "ts": event.get("timestamp"),
        "summary": excerpt(summary, 60),
    }


def compact_reuse_candidate_row(candidate: dict[str, Any]) -> dict[str, Any]:
    """Agent reuse candidate row projection: keep the decision signals + call keys,
    drop the verbose rationale/watermark detail (available via fields="all")."""
    return {
        # Call keys always kept in full: agent_id / cc id for SendMessage, session
        # id for claude --resume.
        "agent_id": candidate.get("agent_id"),
        "cc_tool_use_id": candidate.get("cc_tool_use_id"),
        "session_id": candidate.get("session_id"),
        # Selection signals for the three-way decision.
        "name": candidate.get("name"),
        "role": candidate.get("role"),
        "domain_match": candidate.get("domain_match"),
        "ctx_pct": candidate.get("ctx_pct"),
        "ctx_tokens": candidate.get("ctx_tokens"),
        "availability": candidate.get("availability"),
        "recommended_action": candidate.get("recommended_action"),
        # Actionable next step (holds the addressing id; kept whole, not excerpted).
        "resume_hint": candidate.get("resume_hint"),
    }


def compact_agent_row(agent: dict[str, Any]) -> dict[str, Any]:
    """Team member row projection: call keys + the who-is-doing-what signals.

    Dropped here (all reachable via fields="all"): system_prompt - 24.7% of the
    measured payload on its own - plus config, the ctx_* watermark, the token
    ledger, trust_score and the transcript pointer. Kept whole because follow-up
    calls need them verbatim: ``id`` (agent_update_status) and ``name``
    (SendMessage addresses teammates by name).
    """
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "role": agent.get("role"),
        "status": agent.get("status"),
        "current_task": excerpt(agent.get("current_task"), 80),
        "last_active_at": agent.get("last_active_at"),
    }


def minimal_agent_row(agent: dict[str, Any]) -> dict[str, Any]:
    """Offline member digest row: identity plus when it went quiet, nothing else.

    An offline row is a terminated process: it cannot be messaged and cannot be
    assigned work, so the only questions it still answers are "who was here" and
    "when". current_task is deliberately absent - on an offline row it is stale
    by definition and reads as if the agent were still working.
    """
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "role": agent.get("role"),
        "last_active_at": agent.get("last_active_at"),
    }


def compact_template_row(template: dict[str, Any]) -> dict[str, Any]:
    """Agent template row projection: what you need to pick a subagent_type.

    body_preview and the per-template disallowedTools array are the weight here;
    the tool面 that matters when choosing a template is name + what it is for.
    """
    return {
        "name": template.get("name"),
        "desc": excerpt(template.get("description"), 120),
        "model": template.get("model"),
        "source": template.get("source"),
        "restricted": bool(template.get("disallowedTools")),
    }


def compact_wf_agent_row(row: dict[str, Any]) -> dict[str, Any]:
    """Workflow agent telemetry row projection.

    prompt_preview + result_preview were 53% of a measured 268,753-char
    workflow_get response. Identity, phase, cost and state stay; the two preview
    blobs degrade to excerpts; the four near-identical timestamps and the
    constant-per-run keys (run_id / wf_id / project_id) drop out entirely.
    """
    row_out: dict[str, Any] = {
        "label": row.get("label"),
        "state": row.get("state"),
        "model": row.get("model"),
        "phase": row.get("phase_index"),
        "tokens": row.get("tokens"),
        "tool_calls": row.get("tool_calls"),
        "duration_ms": row.get("duration_ms"),
        # Drill-down keys kept whole: os_agent_id joins the OS ledger.
        "os_agent_id": row.get("os_agent_id"),
        "result": excerpt(row.get("result_preview"), 100),
    }
    if row.get("last_tool_name"):
        row_out["last_tool"] = row.get("last_tool_name")
    return row_out


def compact_team_row(team: dict[str, Any]) -> dict[str, Any]:
    """Team list row projection: the keys needed to pick a team and drill in."""
    row: dict[str, Any] = {
        "id": team.get("id"),
        "name": team.get("name"),
        "status": team.get("status"),
        "kind": (team.get("config") or {}).get("kind", ""),
        "project_id": team.get("project_id"),
        "created_at": team.get("created_at"),
    }
    if team.get("summary"):
        row["summary"] = excerpt(team.get("summary"), 80)
    return row


def compact_profile_row(profile: dict[str, Any]) -> dict[str, Any]:
    """生态库档案行投影：扫列表定钻取目标所需的 5 字段。"""
    summary = (
        profile.get("one_line_summary")
        or profile.get("description_excerpt")
        or profile.get("description")
    )
    return {
        "repo": profile.get("repo_full_name"),
        "stars": profile.get("stars"),
        "lang": profile.get("language") or "",
        "status": profile.get("stage_status"),
        "summary": excerpt(summary, 120),
    }


# Even excerpted, an activity row is bounded at roughly 420 chars, so this cap
# keeps the widest legal window near 17k - a window wider than this walks back
# toward the ceiling the projection just pulled us away from.
ACTIVITY_COMPACT_CAP = 40


def compact_activity_row(activity: dict[str, Any]) -> dict[str, Any]:
    """Activity row projection: the untruncated tool I/O blobs become excerpts.

    output_summary routinely carries a whole command transcript (500+ chars a
    row measured), which is what pushed a 60-row window to 43.9k chars. The row
    id is dropped on purpose - no tool takes an activity id, so it is 42 chars a
    row of pure weight; agent_id stays because it is the key you drill down on.
    """
    row: dict[str, Any] = {
        "agent_id": activity.get("agent_id"),
        "tool": activity.get("tool_name"),
        "status": activity.get("status"),
        "ts": activity.get("timestamp"),
        "duration_ms": activity.get("duration_ms"),
        "input": excerpt(activity.get("input_summary"), 80),
        "output": excerpt(activity.get("output_summary"), 100),
    }
    if activity.get("error"):
        row["error"] = excerpt(activity.get("error"), 80)
    return row


# ------------------------------------------------------------------
# 名册投影（agent_list / team_status / team_briefing 共用同一套语义）
# ------------------------------------------------------------------

# The team roster endpoint caps at 200 rows; ask for the whole cap so the status
# counts below describe the real roster instead of one page of it.
ROSTER_FETCH_LIMIT = 200
# Offline rows folded into a count keep this many most-recent entries visible,
# so "who was on this team" never disappears without a trace.
OFFLINE_PREVIEW_DEFAULT = 5


def _last_seen(agent: dict[str, Any]) -> str:
    """Sort key for 'most recently alive first' (missing timestamps sort last)."""
    return str(agent.get("last_active_at") or agent.get("created_at") or "")


def project_roster(
    agents: list[Any],
    *,
    include_offline: bool = False,
    offline_preview: int = OFFLINE_PREVIEW_DEFAULT,
    view: str = "compact",
) -> dict[str, Any]:
    """Split a team roster into "who is here now" plus an offline digest.

    Measured on the real 51-member session container team (2026-08-03): offline
    rows were 96.4% of the agent_list payload and system_prompt alone 24.7%,
    which put both agent_list and team_status past the MCP result ceiling and
    made two of the most-used observability tools fail outright.

    Why offline is the part that folds: an offline agent is a terminated
    process. It cannot be messaged, cannot be assigned work, and its
    current_task is stale - it earns the least decision value per byte of any
    row in the payload. Nothing is deleted: the count is always reported, the
    most recent entries stay visible, and include_offline=True brings the whole
    roster back (agents rows are an audit ledger and are never dropped).

    Returns a dict with members (projected rows honouring `view`), counted,
    status_counts, and - unless include_offline - an offline digest block.
    """
    rows = [a for a in agents if isinstance(a, dict)]
    counts: dict[str, int] = {}
    for agent in rows:
        key = str(agent.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    offline = [a for a in rows if str(a.get("status") or "") == "offline"]
    listed = rows if include_offline else [a for a in rows if str(a.get("status") or "") != "offline"]
    project = (lambda a: a) if view == "all" else compact_agent_row
    out: dict[str, Any] = {
        "counted": len(rows),
        "status_counts": counts,
        "members": [project(a) for a in listed],
    }
    if not include_offline:
        preview = sorted(offline, key=_last_seen, reverse=True)[: max(0, offline_preview)]
        out["offline"] = {
            "count": len(offline),
            "recent": [minimal_agent_row(a) for a in preview],
        }
    return out


def page(rows: list[Any], limit: int, offset: int, cap: int = ROSTER_FETCH_LIMIT) -> tuple[list[Any], int, bool]:
    """Clamp+slice a projected row list. Returns (page, effective_limit, has_more)."""
    size = max(1, min(int(limit or 1), cap))
    start = max(0, int(offset or 0))
    end = start + size
    return rows[start:end], size, end < len(rows)


# ------------------------------------------------------------------
# 自标识 hint（用户裁定：必须让 agent 知道这是精简版而非字段缺失）
# ------------------------------------------------------------------

TASK_WALL_HINT = (
    "精简视图（非字段缺失）：单任务全量用 task_status(task_id)、"
    '历史进展用 task_memo_read(task_id)；本工具 fields="all" 返回全字段'
)
EVENT_HINT = (
    "精简视图（非字段缺失）：summary 由事件 payload 派生；"
    '完整 payload 用 fields="all"'
)
ECO_LIST_HINT = (
    "精简视图（非字段缺失）：单仓完整档案用 ecosystem_repo_get(repo_full_name)；"
    '本工具 fields="all" 返回全字段'
)
REUSE_HINT = (
    "精简视图（非字段缺失）：候选决策信号"
    "(domain_match/ctx_pct/availability/recommended_action)与调用键已保留；"
    '完整理由(rationale)与水位明细用 fields="all"'
)
# 名册类逃生舱统一话术：offline 只是折叠成计数，行还在库里（agents 表禁删）。
_ROSTER_ESCAPE = (
    "offline 成员已折成计数+最近几位摘要（行仍在库，只是没展开）："
    "完整历史名册用 include_offline=True，"
    "想找可复用的历史成员用 agent_reuse_recommend(query=...)"
)
AGENT_LIST_HINT = (
    "精简视图（非字段缺失）：已隐去 system_prompt/config/上下文水位/用量等大字段，"
    "调用键 id（改状态用）与 name（SendMessage 寻址用）完整保留；"
    f'{_ROSTER_ESCAPE}；全字段用 fields="all"'
)
TEAM_STATUS_HINT = (
    "精简视图（非字段缺失）：members 与 active_tasks 均为投影行；"
    f"{_ROSTER_ESCAPE}；"
    '单任务全量用 task_status(task_id)、整队全字段用 fields="all"'
)
TEAM_LIST_HINT = (
    "精简视图（非字段缺失）：每队只保留 id/name/status/kind/project_id/created_at；"
    "单队详情用 team_status(team_id)；全字段用 fields=\"all\"（316 队全量实测 14.8 万字符，会超上限）"
)
TEAM_BRIEFING_HINT = (
    "精简视图（非字段缺失）：成员名册、最近事件、待办任务均为投影行；"
    f"{_ROSTER_ESCAPE}；全字段用 fields=\"all\""
)
ACTIVITY_HINT = (
    "精简视图（非字段缺失）：input/output 已截断为摘要（原文可能是整段命令输出）；"
    '完整记录用 fields="all"（实测 60 条全量 4.4 万字符，逼近 MCP 上限）'
)
TEMPLATE_LIST_HINT = (
    "精简视图（非字段缺失）：每个模板只留 name/desc/model/source/restricted，"
    "grouped 只给分类到模板名的索引；"
    '完整正文预览与 disallowedTools 明细用 fields="all"（实测全量 3.2 万字符）'
)
WORKFLOW_GET_HINT = (
    "精简视图（非字段缺失）：run.result/summary 与每个 agent 的 prompt/result "
    "预览已截断为摘要，agent 行只留身份/阶段/成本/状态与 os_agent_id 钻取键；"
    '完整存档用 fields="all"（实测 166 个 agent 的全量档案 26.9 万字符，必超 MCP 上限）'
)
