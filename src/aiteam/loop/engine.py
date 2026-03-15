"""AI Team OS — 公司循环引擎.

LoopEngine是纯规则驱动的状态机，不是后台进程。
由Leader通过MCP tools调用触发，每次调用执行一步状态转换。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiteam.types import (
    LoopPhase,
    LoopState,
    Task,
    TaskHorizon,
    TaskPriority,
    TaskStatus,
)

logger = logging.getLogger(__name__)

# 状态转换规则表
TRANSITIONS: dict[LoopPhase, dict[str, LoopPhase]] = {
    LoopPhase.IDLE: {
        "start": LoopPhase.PLANNING,
    },
    LoopPhase.PLANNING: {
        "tasks_planned": LoopPhase.EXECUTING,
    },
    LoopPhase.EXECUTING: {
        "batch_completed": LoopPhase.MONITORING,
        "all_tasks_done": LoopPhase.REVIEWING,
    },
    LoopPhase.MONITORING: {
        "issues_found": LoopPhase.EXECUTING,
        "all_clear": LoopPhase.REVIEWING,
    },
    LoopPhase.REVIEWING: {
        "new_tasks_added": LoopPhase.PLANNING,
        "no_more_tasks": LoopPhase.IDLE,
    },
    LoopPhase.PAUSED: {
        "resume": LoopPhase.IDLE,  # 动态替换为prev_phase
    },
}

# 优先级权重
PRIORITY_WEIGHTS = {
    TaskPriority.CRITICAL: 100,
    TaskPriority.HIGH: 40,
    TaskPriority.MEDIUM: 10,
    TaskPriority.LOW: 2,
}

HORIZON_WEIGHTS = {
    TaskHorizon.SHORT: 3.0,
    TaskHorizon.MID: 1.5,
    TaskHorizon.LONG: 0.8,
}


def calculate_task_score(task: Task, now: datetime | None = None) -> float:
    """计算任务的综合排序分数，越高越优先."""
    if now is None:
        now = datetime.now()

    if task.status not in (TaskStatus.PENDING,):
        return 0.0

    priority_w = PRIORITY_WEIGHTS.get(
        TaskPriority(task.priority) if isinstance(task.priority, str) else task.priority,
        10,
    )
    horizon_w = HORIZON_WEIGHTS.get(
        TaskHorizon(task.horizon) if isinstance(task.horizon, str) else task.horizon,
        1.0,
    )

    # 被阻塞大幅降权
    if task.status == TaskStatus.BLOCKED:
        readiness = 0.1
    else:
        readiness = 1.0

    # 时间衰减（越久未处理分数略升，防饿死）
    age_hours = (now - task.created_at).total_seconds() / 3600
    age_boost = 1.0 + min(age_hours / 168, 0.5)

    # pinned标签置顶
    pinned_boost = 1000.0 if "pinned" in (task.tags or []) else 0.0

    return priority_w * horizon_w * readiness * age_boost + pinned_boost


class LoopEngine:
    """公司循环引擎 — 纯规则驱动，无LLM依赖."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def get_state(self, team_id: str) -> LoopState:
        """获取或创建循环状态."""
        from sqlalchemy import text

        from aiteam.storage.connection import get_session

        db_url = self._repo._db_url
        async with get_session(db_url) as session:
            result = await session.execute(
                text("SELECT * FROM loop_states WHERE team_id = :tid"),
                {"tid": team_id},
            )
            row = result.mappings().first()
            if row:
                return LoopState(
                    team_id=row["team_id"],
                    phase=LoopPhase(row["phase"]),
                    prev_phase=LoopPhase(row["prev_phase"]) if row.get("prev_phase") else None,
                    current_cycle=row["current_cycle"] or 0,
                    completed_tasks_count=row["completed_tasks_count"] or 0,
                    current_task_id=row.get("current_task_id"),
                    review_interval=row["review_interval"] or 5,
                )

        # 不存在则创建
        return await self._create_state(team_id)

    async def _create_state(self, team_id: str) -> LoopState:
        """创建初始循环状态."""
        from sqlalchemy import text

        from aiteam.storage.connection import get_session

        state = LoopState(team_id=team_id)
        db_url = self._repo._db_url
        async with get_session(db_url) as session:
            await session.execute(
                text("""INSERT OR REPLACE INTO loop_states
                     (team_id, phase, current_cycle, completed_tasks_count, review_interval, updated_at)
                     VALUES (:tid, :phase, 0, 0, 5, :now)"""),
                {"tid": team_id, "phase": state.phase.value, "now": datetime.now().isoformat()},
            )
        return state

    async def _save_state(self, state: LoopState) -> None:
        """持久化循环状态."""
        from sqlalchemy import text

        from aiteam.storage.connection import get_session

        db_url = self._repo._db_url
        async with get_session(db_url) as session:
            await session.execute(
                text("""UPDATE loop_states SET
                     phase=:phase, prev_phase=:prev, current_cycle=:cycle,
                     completed_tasks_count=:count, current_task_id=:task,
                     review_interval=:interval, updated_at=:now
                     WHERE team_id=:tid"""),
                {
                    "tid": state.team_id,
                    "phase": state.phase.value,
                    "prev": state.prev_phase.value if state.prev_phase else None,
                    "cycle": state.current_cycle,
                    "count": state.completed_tasks_count,
                    "task": state.current_task_id,
                    "interval": state.review_interval,
                    "now": datetime.now().isoformat(),
                },
            )

    async def start(self, team_id: str) -> LoopState:
        """启动公司循环."""
        state = await self.get_state(team_id)
        state.phase = LoopPhase.PLANNING
        state.current_cycle += 1
        await self._save_state(state)
        logger.info("Loop started: team=%s, cycle=%d", team_id, state.current_cycle)
        return state

    async def advance(self, team_id: str, trigger: str) -> LoopState:
        """根据触发器推进循环阶段."""
        state = await self.get_state(team_id)

        transitions = TRANSITIONS.get(state.phase, {})
        next_phase = transitions.get(trigger)

        if next_phase is None:
            msg = f"无效的状态转换: {state.phase.value} + {trigger}"
            raise ValueError(msg)

        # 特殊处理：pause恢复到prev_phase
        if state.phase == LoopPhase.PAUSED and trigger == "resume":
            next_phase = state.prev_phase or LoopPhase.PLANNING

        old_phase = state.phase
        state.phase = next_phase
        await self._save_state(state)
        logger.info("Loop advanced: %s → %s (trigger=%s)", old_phase.value, next_phase.value, trigger)
        return state

    async def pause(self, team_id: str) -> LoopState:
        """暂停循环."""
        state = await self.get_state(team_id)
        state.prev_phase = state.phase
        state.phase = LoopPhase.PAUSED
        await self._save_state(state)
        return state

    async def resume(self, team_id: str) -> LoopState:
        """恢复循环."""
        state = await self.get_state(team_id)
        if state.prev_phase:
            state.phase = state.prev_phase
            state.prev_phase = None
        else:
            state.phase = LoopPhase.PLANNING
        await self._save_state(state)
        return state

    async def get_next_task(self, team_id: str, agent_id: str | None = None) -> Task | None:
        """获取下一个应执行的任务（按score排序）."""
        all_tasks = await self._repo.list_tasks(team_id, status=TaskStatus.PENDING)

        if not all_tasks:
            return None

        now = datetime.now()
        scored = [(calculate_task_score(t, now), t) for t in all_tasks]
        scored.sort(key=lambda x: x[0], reverse=True)

        # 如果指定了agent_id，优先返回已分配给该agent的任务
        if agent_id:
            for score, task in scored:
                if task.assigned_to == agent_id:
                    return task

        return scored[0][1] if scored else None

    async def on_task_completed(self, team_id: str) -> LoopState:
        """任务完成后更新循环状态."""
        state = await self.get_state(team_id)
        state.completed_tasks_count += 1
        state.current_task_id = None

        # 检查是否需要触发回顾
        if state.completed_tasks_count % state.review_interval == 0:
            state.phase = LoopPhase.REVIEWING

        await self._save_state(state)
        return state

    async def start_review(self, team_id: str) -> dict[str, Any]:
        """触发回顾：创建回顾会议，生成统计报告."""
        # 1. 获取本轮任务统计
        all_tasks = await self._repo.list_tasks(team_id)
        completed = [t for t in all_tasks if t.status == TaskStatus.COMPLETED]
        failed = [t for t in all_tasks if t.status == TaskStatus.FAILED]
        pending = [t for t in all_tasks if t.status == TaskStatus.PENDING]
        running = [t for t in all_tasks if t.status == TaskStatus.RUNNING]
        blocked = [t for t in all_tasks if t.status == TaskStatus.BLOCKED]

        # 2. 获取 open issues
        open_issues = [
            t for t in all_tasks
            if t.config.get("task_type") == "issue"
            and t.status not in (TaskStatus.COMPLETED,)
        ]

        # 3. 生成议程文本
        agenda_lines = [
            "# 公司循环回顾报告",
            "",
            "## 任务统计",
            f"- 总任务数: {len(all_tasks)}",
            f"- 已完成: {len(completed)}",
            f"- 失败: {len(failed)}",
            f"- 进行中: {len(running)}",
            f"- 待处理: {len(pending)}",
            f"- 被阻塞: {len(blocked)}",
            "",
        ]

        if completed:
            agenda_lines.append("## 已完成的任务")
            for t in completed:
                agenda_lines.append(f"- [{t.priority}] {t.title or t.description[:60]}")
            agenda_lines.append("")

        if failed:
            agenda_lines.append("## 失败的任务（需分析原因）")
            for t in failed:
                result_hint = ""
                if t.result:
                    result_hint = f" — {t.result[:80]}"
                agenda_lines.append(f"- [{t.priority}] {t.title or t.description[:60]}{result_hint}")
            agenda_lines.append("")

        if open_issues:
            agenda_lines.append("## 未解决的 Issue")
            for t in open_issues:
                severity = t.config.get("severity", "unknown")
                category = t.config.get("category", "")
                agenda_lines.append(f"- [{severity}/{category}] {t.title or t.description[:60]}")
            agenda_lines.append("")

        agenda_lines.extend([
            "## 讨论议程",
            "1. 本轮完成情况回顾",
            "2. 失败任务原因分析与对策",
            "3. 未解决 Issue 处理计划",
            "4. 下一步工作建议",
        ])

        agenda_text = "\n".join(agenda_lines)

        # 4. 创建回顾会议
        state = await self.get_state(team_id)
        topic = f"公司循环回顾 — 第 {state.current_cycle} 周期"
        meeting = await self._repo.create_meeting(team_id, topic=topic, participants=[])

        # 5. 发送统计报告作为第一条消息
        await self._repo.create_meeting_message(
            meeting_id=meeting.id,
            agent_id="system",
            agent_name="LoopEngine",
            content=agenda_text,
            round_number=1,
        )

        logger.info("Review started: team=%s, meeting=%s", team_id, meeting.id)

        return {
            "meeting_id": meeting.id,
            "topic": topic,
            "cycle": state.current_cycle,
            "stats": {
                "total": len(all_tasks),
                "completed": len(completed),
                "failed": len(failed),
                "running": len(running),
                "pending": len(pending),
                "blocked": len(blocked),
                "open_issues": len(open_issues),
            },
        }

    async def get_task_wall(
        self, team_id: str, horizon: str = "", priority: str = "",
    ) -> dict[str, Any]:
        """获取任务墙视图."""
        all_tasks = await self._repo.list_tasks(team_id)

        now = datetime.now()
        # 计算score并按horizon分组
        wall: dict[str, list[dict]] = {"short": [], "mid": [], "long": []}
        completed_tasks: list[dict] = []

        for task in all_tasks:
            if task.status == TaskStatus.COMPLETED:
                completed_tasks.append(task.model_dump(mode="json"))
                continue

            h = task.horizon if isinstance(task.horizon, str) else task.horizon.value
            if horizon and h != horizon:
                continue

            p = task.priority if isinstance(task.priority, str) else task.priority.value
            if priority and p not in priority.split(","):
                continue

            score = calculate_task_score(task, now)
            item = task.model_dump(mode="json")
            item["score"] = round(score, 1)

            if h in wall:
                wall[h].append(item)

        # 每组内按score降序
        for key in wall:
            wall[key].sort(key=lambda x: x["score"], reverse=True)

        # 已完成任务按完成时间降序
        completed_tasks.sort(
            key=lambda x: x.get("completed_at") or "", reverse=True,
        )

        stats = {
            "total": len(all_tasks),
            "by_status": {},
            "completed_count": len(completed_tasks),
        }
        for task in all_tasks:
            s = task.status if isinstance(task.status, str) else task.status.value
            stats["by_status"][s] = stats["by_status"].get(s, 0) + 1

        return {"wall": wall, "completed": completed_tasks, "stats": stats}
