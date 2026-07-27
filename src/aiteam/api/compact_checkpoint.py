"""压缩检查点 — 上下文被压缩前，把 OS 侧的作战态拍一张快照。

CC 压缩会话时，模型自己的记忆由 CC 的 compact_summary 接管；**丢掉的是 OS 这
一侧的运营态**：这个会话派了哪些 agent、它们在做什么、项目墙上什么还开着、
有哪些待决事项挂在缔造者那里。这些东西一直都在库里，压缩后的 Leader 只是不
知道该去问。检查点做的就是把它们在压缩那一刻定格，压缩后的第一次 SessionStart
（source=compact）原样递回去。

刻意不存 CC 的 compact_summary：那段摘要**压缩后本来就在模型上下文里**，再注
入一遍是重复；OS 该补的是模型没有的那半边。

存储用 events 表（``session.compact_checkpoint``，按 ``session:<id>`` 定位）：
压缩是低频事件（本机 23 天 159 次），不值得为它单开一张表和一次迁移，而事件流
天然带时间戳、天然 append-only，正合检查点的语义。
"""

from __future__ import annotations

import logging
from typing import Any

from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskStatus

logger = logging.getLogger(__name__)

CHECKPOINT_EVENT = "session.compact_checkpoint"

_MAX_AGENTS = 12
_MAX_TASKS = 10
_MAX_BRIEFINGS = 5


async def build_snapshot(
    repo: StorageRepository, session_id: str, cwd: str = ""
) -> dict[str, Any]:
    """把这个会话此刻的 OS 侧作战态收成一个可 JSON 化的快照。

    每一块都单独兜底：某一块查不出来就留空，绝不让一次探测失败把整张检查点搞
    没——压缩这一刻拿不到第二次机会。
    """
    snapshot: dict[str, Any] = {
        "session_id": session_id,
        "cwd": cwd,
        "leader": None,
        "agents": [],
        "open_tasks": [],
        "pending_briefings": [],
    }

    project_id = ""
    try:
        agents = await repo.find_agents_by_session(session_id)
        for agent in agents:
            if agent.role == "leader":
                snapshot["leader"] = {"name": agent.name, "model": agent.model or ""}
                project_id = agent.project_id or ""
                continue
            if agent.status.value == "offline":
                continue
            snapshot["agents"].append(
                {
                    "name": agent.name,
                    "status": agent.status.value,
                    "current_task": agent.current_task or "",
                    "ctx_pct": agent.ctx_pct,
                }
            )
        snapshot["agents"] = snapshot["agents"][:_MAX_AGENTS]
    except Exception:  # noqa: BLE001
        logger.debug("compact checkpoint: agent snapshot failed", exc_info=True)

    if project_id:
        snapshot["project_id"] = project_id
        try:
            running = await repo.list_tasks_by_project(project_id, status=TaskStatus.RUNNING)
            pending = await repo.list_tasks_by_project(project_id, status=TaskStatus.PENDING)
            for task in (running + pending)[:_MAX_TASKS]:
                snapshot["open_tasks"].append(
                    {
                        "id": task.id,
                        "title": task.title,
                        "status": task.status.value,
                        "assigned_to": task.assigned_to or "",
                    }
                )
        except Exception:  # noqa: BLE001
            logger.debug("compact checkpoint: task snapshot failed", exc_info=True)

        try:
            briefings = await repo.list_briefings(status="pending", project_id=project_id)
            for item in briefings[:_MAX_BRIEFINGS]:
                snapshot["pending_briefings"].append(
                    {"id": item.id, "title": item.title, "urgency": item.urgency}
                )
        except Exception:  # noqa: BLE001
            logger.debug("compact checkpoint: briefing snapshot failed", exc_info=True)

    return snapshot


def render(snapshot: dict[str, Any]) -> str:
    """把快照渲染成压缩后直接注入 Leader 上下文的文本块。

    空快照返回空串——没东西可说的时候不占用户的上下文。
    """
    agents = snapshot.get("agents") or []
    tasks = snapshot.get("open_tasks") or []
    briefings = snapshot.get("pending_briefings") or []
    if not (agents or tasks or briefings):
        return ""

    lines = ["", "## 压缩前的作战态（OS 检查点）", ""]
    if agents:
        lines.append(f"在飞 Agent（{len(agents)}）：")
        for a in agents:
            task = f" — {a['current_task']}" if a.get("current_task") else ""
            ctx = f"（上下文 {a['ctx_pct']:.0%}）" if a.get("ctx_pct") else ""
            lines.append(f"  - {a['name']} [{a['status']}]{task}{ctx}")
        lines.append("")
    if tasks:
        lines.append(f"未完成任务（{len(tasks)}）：")
        for t in tasks:
            owner = f" @{t['assigned_to']}" if t.get("assigned_to") else ""
            lines.append(f"  - [{t['status']}] {t['title']}{owner}")
        lines.append("")
    if briefings:
        lines.append(f"待缔造者裁决（{len(briefings)}）：")
        for b in briefings:
            lines.append(f"  - [{b['urgency']}] {b['title']}（id {b['id'][:8]}）")
        lines.append("")
    lines.append(
        "以上是压缩那一刻 OS 库里的实况，不是回忆。要细节请用 task_memo_read / "
        "briefing_list / taskwall_view 现查，别凭这段摘要推断。"
    )
    return "\n".join(lines)
