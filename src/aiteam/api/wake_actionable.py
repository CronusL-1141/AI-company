"""Wake actionable predicate — the single source of truth for "is there work in
flight the Leader should react to".

唤醒体系 v2（docs/wake-loop-v2-design.md §7.1/§7.2）的判据基石。事件 watcher 轮询它、
turn-end guard 停机时查它，二者共用同一判据，对"有无活在飞"永不打架。判据集中在
Python（可单测），bash watcher 只当哑轮询器（零 SQL）。

设计原则（对齐 session_probe.py）：纯只读、防御式——每个子信号独立 try 包裹，任何失败
一律降级为 0/空，绝不抛出、绝不 500。宁可少报（下一 /loop tick 兜底）不可让 watcher/guard
因端点异常而失联。

数据键映射（照数据实际归属，不臆造）：
- agents 按 team_id（Leader 会话团队，子 agent 挂其下）
- workflow_runs 按 session_id（WorkflowRun.session_id = 启动的 Leader 会话；run 自带
  workflow-<id> 团队 ≠ Leader 团队，故绝不能按 team_id 过滤 runs）
- task_memos / briefings 按 project_id（从 team 解析）

时间口径：DB 存 naive-local（datetime.now()）。since 一律解析并归一到 naive-local；
watermark 返回 datetime.now().isoformat()（本地、无 Z），watcher 原样回传形成单调水位。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiteam.types import AgentStatus

logger = logging.getLogger(__name__)

# workflow_runs 的终态集合（design §7.2）：这些是 actionable 的（尤其后三者需补救）。
TERMINAL_RUN_STATUSES = frozenset({"completed", "interrupted", "killed", "failed"})
# 活跃态：仍在飞，属良性（benign），不触发唤醒，只作 guard 的"有无活在飞"计量。
LIVE_RUN_STATUSES = frozenset({"planned", "running"})

# reasons 列表上限，防响应膨胀
_MAX_REASONS = 12


def parse_since(raw: str | None) -> datetime | None:
    """把 since 查询参数解析为 naive-local datetime；无法解析返回 None（=不设下界）。

    容错接受：带 'Z'、带时区偏移、或纯 naive 的 ISO8601。带时区者转本地后去 tzinfo，
    与 DB 的 naive-local 时间戳对齐比较。
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        # fromisoformat 在 3.11+ 支持 'Z'；老版本手动替换兜底
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _after(ts: datetime | None, since: datetime | None) -> bool:
    """ts 是否严格晚于 since；since 为 None 视为无下界（一律计入）。ts 缺失则不计入。"""
    if ts is None:
        return False
    if since is None:
        return True
    try:
        return ts > since
    except TypeError:
        # 极端：一方带 tzinfo。归一到 naive 再比。
        try:
            a = ts.replace(tzinfo=None)
            b = since.replace(tzinfo=None)
            return a > b
        except Exception:  # noqa: BLE001
            return False


async def _resolve_project_id(repo: Any, team_id: str, project_id: str) -> str:
    """project_id 缺省时从 team 解析。失败返回空串（memo/briefing 信号将降级为 0）。"""
    if project_id:
        return project_id
    if not team_id:
        return ""
    try:
        team = await repo.get_team(team_id)
        return (team.project_id or "") if team else ""
    except Exception:  # noqa: BLE001
        return ""


async def _agent_signals(
    repo: Any, team_id: str, session_id: str, since: datetime | None
) -> tuple[int, int, list[str]]:
    """返回 (busy_agents, finished_agents_since, reasons)。

    finished_agents_since 是近似：AgentModel 无"状态变更时间戳"，故以
    status ∈ {waiting, offline} 且 last_active_at > since 近似"刚从 busy 收工"。
    这是次要信号——子 agent 完成通常会写 task_memo，由 new_memos_since 强兜底。
    """
    reasons: list[str] = []
    try:
        agents = []
        if team_id:
            agents = await repo.list_agents(team_id)
        elif session_id:
            agents = await repo.find_agents_by_session(session_id)
        busy = 0
        finished = 0
        for a in agents:
            status = getattr(a, "status", None)
            if status == AgentStatus.BUSY:
                busy += 1
            elif status in (AgentStatus.WAITING, AgentStatus.OFFLINE):
                if _after(getattr(a, "last_active_at", None), since):
                    finished += 1
                    if len(reasons) < _MAX_REASONS:
                        reasons.append(f"agent '{getattr(a, 'name', '?')}' 收工（待接力）")
        return busy, finished, reasons
    except Exception:  # noqa: BLE001 — probe failure must not break the endpoint
        logger.warning("wake_actionable: agent signal failed", exc_info=True)
        return 0, 0, reasons


async def _run_signals(
    repo: Any, session_id: str, project_id: str, since: datetime | None
) -> tuple[int, int, list[str]]:
    """返回 (live_runs, terminal_runs_since, reasons)，按 session_id 归属过滤。"""
    reasons: list[str] = []
    try:
        runs = await repo.list_workflow_runs(project_id=project_id, limit=50)
        live = 0
        terminal = 0
        for r in runs:
            # 只认这个 Leader 会话启动的 run（session_id 为空则不按会话收窄）
            if session_id and getattr(r, "session_id", None) not in (session_id, None):
                continue
            status = getattr(r, "status", "")
            if status in LIVE_RUN_STATUSES:
                live += 1
            elif status in TERMINAL_RUN_STATUSES:
                if _after(getattr(r, "updated_at", None), since):
                    terminal += 1
                    if len(reasons) < _MAX_REASONS:
                        wf = getattr(r, "wf_id", "?")
                        reasons.append(f"run {wf} → {status}")
        return live, terminal, reasons
    except Exception:  # noqa: BLE001
        logger.warning("wake_actionable: run signal failed", exc_info=True)
        return 0, 0, reasons


async def _memo_count(repo: Any, project_id: str, since: datetime | None) -> int:
    """since 之后新增的有效 task_memo 数（subagent 报进展/总结）。"""
    if not project_id:
        return 0
    try:
        return int(await repo.count_valid_task_memos_since(project_id, since))
    except Exception:  # noqa: BLE001
        logger.warning("wake_actionable: memo signal failed", exc_info=True)
        return 0


async def _pending_briefings(repo: Any, project_id: str) -> int:
    """待决简报数——面向用户的信号，仅展示，不触发 Leader 自身唤醒（design §7.2）。"""
    try:
        briefings = await repo.list_briefings(status="pending", project_id=project_id)
        return len(briefings)
    except Exception:  # noqa: BLE001
        logger.warning("wake_actionable: briefing signal failed", exc_info=True)
        return 0


async def compute_actionable(
    repo: Any,
    session_id: str = "",
    team_id: str = "",
    project_id: str = "",
    since_raw: str | None = None,
) -> dict:
    """计算唤醒判据。绝不抛出：任何内部失败降级为保守值。

    actionable = finished_agents_since>0 OR terminal_runs_since>0 OR new_memos_since>0
    （briefings 不计入触发；busy_agents/live_runs 供 guard 判"有无活在飞"）。
    """
    since = parse_since(since_raw)
    resolved_project = await _resolve_project_id(repo, team_id, project_id)

    busy_agents, finished_agents, agent_reasons = await _agent_signals(
        repo, team_id, session_id, since
    )
    live_runs, terminal_runs, run_reasons = await _run_signals(
        repo, session_id, resolved_project, since
    )
    new_memos = await _memo_count(repo, resolved_project, since)
    pending_briefings = await _pending_briefings(repo, resolved_project)

    reasons = agent_reasons + run_reasons
    if new_memos > 0:
        reasons.append(f"{new_memos} 条新 task_memo（子 agent 报进展）")

    actionable = (finished_agents > 0) or (terminal_runs > 0) or (new_memos > 0)

    return {
        "actionable": actionable,
        "reasons": reasons[:_MAX_REASONS],
        "busy_agents": busy_agents,
        "live_runs": live_runs,
        "terminal_runs_since": terminal_runs,
        "finished_agents_since": finished_agents,
        "new_memos_since": new_memos,
        "pending_briefings": pending_briefings,
        "project_id": resolved_project,
        "watermark": datetime.now().isoformat(),
    }
