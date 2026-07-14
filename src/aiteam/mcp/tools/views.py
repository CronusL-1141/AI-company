"""列表类工具返回体的精简投影视图（表示层，不动 API 路由与数据层）。

设计规格见 docs/tool-loading-design.md §4。要点（基准任务 4267426d 实测背书）：

- 默认 ``fields="compact"`` 返回精简投影：task-wall 省 84.3% / events 省
  50.7% / ecosystem 列表省 80.7% token；``fields="all"`` 为逃生舱返回全量。
- 返回体携带 ``view`` + ``hint`` 自标识——让 agent 明确知道这是精简视图
  而非字段缺失，并指路单体详情工具（用户裁定 2026-07-14）。
- 投影三铁律：后续调用要用的键（id）永远完整；语义内容只降级为截断摘要、
  不删除；选择动作用得上的信号字段（score/assigned_to/depends_on 等）保留。
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
