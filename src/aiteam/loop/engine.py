"""AI Team OS — Company loop engine.

LoopEngine is a pure rule-driven state machine, not a background process.
Triggered by the Leader via MCP tools; each call executes one state transition.
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

# State transition rule table
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
        "resume": LoopPhase.IDLE,  # Dynamically replaced with prev_phase
    },
}

# Priority weights
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
    """Calculate composite sorting score for a task; higher means higher priority."""
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

    readiness = 1.0

    # Time decay (score rises slightly the longer a task waits, preventing starvation)
    age_hours = (now - task.created_at).total_seconds() / 3600
    age_boost = 1.0 + min(age_hours / 168, 0.5)

    # Pinned tag boosts task to the top
    pinned_boost = 1000.0 if "pinned" in (task.tags or []) else 0.0

    return priority_w * horizon_w * readiness * age_boost + pinned_boost


class LoopEngine:
    """Company loop engine — pure rule-driven, no LLM dependency."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def get_state(self, team_id: str) -> LoopState:
        """Get or create loop state."""
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

        # Does not exist, create new
        return await self._create_state(team_id)

    async def _create_state(self, team_id: str) -> LoopState:
        """Create initial loop state."""
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
        """Persist loop state."""
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
        """Start the company loop."""
        state = await self.get_state(team_id)
        state.phase = LoopPhase.PLANNING
        state.current_cycle += 1
        await self._save_state(state)
        logger.info("Loop started: team=%s, cycle=%d", team_id, state.current_cycle)
        return state

    async def advance(self, team_id: str, trigger: str) -> LoopState:
        """Advance the loop phase based on a trigger."""
        state = await self.get_state(team_id)

        transitions = TRANSITIONS.get(state.phase, {})
        next_phase = transitions.get(trigger)

        if next_phase is None:
            msg = f"Invalid state transition: {state.phase.value} + {trigger}"
            raise ValueError(msg)

        # Special handling: resume from pause restores prev_phase
        if state.phase == LoopPhase.PAUSED and trigger == "resume":
            next_phase = state.prev_phase or LoopPhase.PLANNING

        old_phase = state.phase
        state.phase = next_phase
        await self._save_state(state)
        logger.info("Loop advanced: %s → %s (trigger=%s)", old_phase.value, next_phase.value, trigger)
        return state

    async def pause(self, team_id: str) -> LoopState:
        """Pause the loop."""
        state = await self.get_state(team_id)
        state.prev_phase = state.phase
        state.phase = LoopPhase.PAUSED
        await self._save_state(state)
        return state

    async def resume(self, team_id: str) -> LoopState:
        """Resume the loop."""
        state = await self.get_state(team_id)
        if state.prev_phase:
            state.phase = state.prev_phase
            state.prev_phase = None
        else:
            state.phase = LoopPhase.PLANNING
        await self._save_state(state)
        return state

    async def get_next_task(self, team_id: str, agent_id: str | None = None) -> Task | None:
        """Get the next task to execute (sorted by score)."""
        all_tasks = await self._repo.list_tasks(team_id, status=TaskStatus.PENDING)

        if not all_tasks:
            return None

        now = datetime.now()
        scored = [(calculate_task_score(t, now), t) for t in all_tasks]
        scored.sort(key=lambda x: x[0], reverse=True)

        # If agent_id is specified, prioritize tasks already assigned to that agent
        if agent_id:
            for score, task in scored:
                if task.assigned_to == agent_id:
                    return task

        return scored[0][1] if scored else None

    async def on_task_completed(self, team_id: str) -> LoopState:
        """Update loop state after a task is completed."""
        state = await self.get_state(team_id)
        state.completed_tasks_count += 1
        state.current_task_id = None

        # Check whether a review should be triggered
        if state.completed_tasks_count % state.review_interval == 0:
            state.phase = LoopPhase.REVIEWING

        await self._save_state(state)
        return state

    async def start_review(self, team_id: str) -> dict[str, Any]:
        """Trigger a review: create a review meeting and generate a statistics report."""
        # 1. Get task statistics for this cycle
        all_tasks = await self._repo.list_tasks(team_id)
        completed = [t for t in all_tasks if t.status == TaskStatus.COMPLETED]
        failed = [t for t in all_tasks if t.status == TaskStatus.FAILED]
        pending = [t for t in all_tasks if t.status == TaskStatus.PENDING]
        running = [t for t in all_tasks if t.status == TaskStatus.RUNNING]
        blocked = [t for t in all_tasks if t.status == TaskStatus.BLOCKED]

        # 2. Get open issues
        open_issues = [
            t for t in all_tasks
            if t.config.get("task_type") == "issue"
            and t.status not in (TaskStatus.COMPLETED,)
        ]

        # 3. Generate agenda text
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

        # 4. Create review meeting
        state = await self.get_state(team_id)
        topic = f"公司循环回顾 — 第 {state.current_cycle} 周期"
        meeting = await self._repo.create_meeting(team_id, topic=topic, participants=[])

        # 5. Send the statistics report as the first message
        await self._repo.create_meeting_message(
            meeting_id=meeting.id,
            agent_id="system",
            agent_name="LoopEngine",
            content=agenda_text,
            round_number=1,
        )

        # Save review as team memory
        try:
            await self._repo.create_memory(
                scope="team",
                scope_id=team_id,
                content=agenda_text,
                metadata={"type": "loop_review", "cycle": state.current_cycle},
            )
        except Exception:
            logger.warning("保存回顾记忆失败: team=%s", team_id)

        # 6. Automatically execute reflection and lesson extraction
        try:
            await self.reflect(team_id, completed, failed, state.current_cycle)
        except Exception:
            logger.warning("reflect执行失败: team=%s", team_id)
        try:
            await self.enrich(team_id, completed, failed)
        except Exception:
            logger.warning("enrich执行失败: team=%s", team_id)

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
        """Get the task wall view."""
        all_tasks = await self._repo.list_tasks(team_id)

        now = datetime.now()
        # Calculate score and group by horizon
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

        # 每组内Sort by score descending
        for key in wall:
            wall[key].sort(key=lambda x: x["score"], reverse=True)

        # Sort completed tasks by completion time descending
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

    async def reflect(
        self,
        team_id: str,
        completed: list[Task],
        failed: list[Task],
        cycle: int,
    ) -> dict[str, Any]:
        """Analyze execution data for this cycle and save a reflection report to memory.

        Statistics: completed/failed task count, average duration, top-producing agents.
        """
        # Calculate average duration (only for tasks with both started_at and completed_at)
        durations: list[float] = []
        for t in completed:
            if t.started_at and t.completed_at:
                elapsed = (t.completed_at - t.started_at).total_seconds() / 60
                durations.append(elapsed)
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        # Count top-producing agents (group by assigned_to)
        agent_output: dict[str, int] = {}
        for t in completed:
            if t.assigned_to:
                agent_output[t.assigned_to] = agent_output.get(t.assigned_to, 0) + 1

        top_agents = sorted(agent_output.items(), key=lambda x: x[1], reverse=True)[:3]

        # Generate reflection report
        lines = [
            f"# 循环反思报告 — 第 {cycle} 周期",
            "",
            f"- 已完成任务: {len(completed)}",
            f"- 失败任务: {len(failed)}",
            f"- 平均任务耗时: {avg_duration:.1f} 分钟" if avg_duration else "- 平均任务耗时: 暂无数据",
        ]
        if top_agents:
            lines.append("- 高产出Agent:")
            for agent_id, count in top_agents:
                lines.append(f"  - {agent_id}: {count} 个任务")

        report = "\n".join(lines)

        try:
            await self._repo.create_memory(
                scope="team",
                scope_id=team_id,
                content=report,
                metadata={"type": "reflect", "cycle": cycle},
            )
        except Exception:
            logger.warning("保存反思记忆失败: team=%s", team_id)

        logger.info("reflect完成: team=%s, cycle=%d, completed=%d, failed=%d",
                    team_id, cycle, len(completed), len(failed))
        return {
            "completed": len(completed),
            "failed": len(failed),
            "avg_duration_minutes": round(avg_duration, 1),
            "top_agents": [{"agent_id": a, "count": c} for a, c in top_agents],
        }

    async def enrich(
        self,
        team_id: str,
        completed: list[Task],
        failed: list[Task],
    ) -> dict[str, Any]:
        """Extract lessons learned and save as team memory (type=lesson_learned).

        Extract failure reasons from failed tasks and reusable patterns from successful ones.
        """
        lessons: list[str] = []

        # Extract failure reasons from failed tasks
        for t in failed:
            reason = ""
            if t.result:
                reason = t.result[:200]
            elif t.config.get("error"):
                reason = str(t.config["error"])[:200]
            title = t.title or t.description[:60]
            if reason:
                lessons.append(f"[失败教训] 任务「{title}」失败原因：{reason}")
            else:
                lessons.append(f"[失败教训] 任务「{title}」失败，原因未记录")

        # Extract reusable patterns from successful tasks (those with tags)
        tag_success: dict[str, int] = {}
        for t in completed:
            for tag in (t.tags or []):
                tag_success[tag] = tag_success.get(tag, 0) + 1
        for tag, count in tag_success.items():
            if count >= 2:
                lessons.append(f"[成功模式] 标签「{tag}」的任务本轮完成{count}个，此类任务执行顺畅")

        if not lessons:
            lessons.append("本周期暂无明显失败教训或可复用经验")

        content = "\n".join(lessons)

        try:
            await self._repo.create_memory(
                scope="team",
                scope_id=team_id,
                content=content,
                metadata={"type": "lesson_learned"},
            )
        except Exception:
            logger.warning("保存经验教训记忆失败: team=%s", team_id)

        logger.info("enrich完成: team=%s, lessons=%d", team_id, len(lessons))
        return {"lessons": lessons}
