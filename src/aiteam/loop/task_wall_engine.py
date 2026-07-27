"""AI Team OS — Task wall backend (scoring + team-scoped wall projection).

Split out of the retired loop state machine (2026-07-27 scheduler slim-down).
The scoring function and the wall projection are the live task-wall backend used
by `GET /api/teams/{team_id}/task-wall`, `GET /api/projects/{id}/task-wall` and
the task-wall MCP tool (task_list_project) — they never belonged to the loop phase machine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aiteam.types import Task, TaskHorizon, TaskPriority, TaskStatus

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


class TaskWallEngine:
    """Task wall projection — pure read model over the task repository."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def get_task_wall(
        self,
        team_id: str,
        horizon: str = "",
        priority: str = "",
    ) -> dict[str, Any]:
        """Get the task wall view."""
        all_tasks = await self._repo.list_tasks(team_id)

        # Build parent_id → children mapping so subtasks can be nested into parent items.
        subtask_id_to_stage: dict[str, dict] = {}
        children_map: dict[str, list] = {}
        for task in all_tasks:
            if task.parent_id:
                children_map.setdefault(task.parent_id, []).append(task)
                subtask_id_to_stage[task.id] = {}

        # Populate stage metadata from parent pipeline configs.
        for task in all_tasks:
            pipeline_cfg = task.config.get("pipeline")
            if not pipeline_cfg:
                continue
            for stage in pipeline_cfg.get("stages", []):
                sid = stage.get("subtask_id")
                if sid and sid in subtask_id_to_stage:
                    subtask_id_to_stage[sid] = stage

        now = datetime.now()
        # Calculate score and group by horizon
        wall: dict[str, list[dict]] = {"short": [], "mid": [], "long": []}
        completed_tasks: list[dict] = []

        for task in all_tasks:
            # Filter out pipeline subtasks — they have a parent_id and should not
            # appear as top-level cards on the task wall.
            if task.parent_id:
                continue

            if task.status == TaskStatus.COMPLETED:
                item_c = task.model_dump(mode="json")
                # Nest subtasks for completed parent tasks.
                child_tasks = children_map.get(task.id, [])
                nested_c: list[dict] = []
                for child in child_tasks:
                    stage_meta = subtask_id_to_stage.get(child.id, {})
                    child_status = child.status if isinstance(child.status, str) else child.status.value
                    nested_c.append({
                        "id": child.id,
                        "title": child.title,
                        "status": child_status,
                        "stage_name": stage_meta.get("name"),
                        "agent_template": stage_meta.get("agent_template"),
                        "completed_at": child.completed_at.isoformat() if child.completed_at else None,
                    })
                item_c["subtasks"] = nested_c
                completed_tasks.append(item_c)
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

            # Attach pipeline progress summary if the task has a pipeline config.
            pipeline_cfg = task.config.get("pipeline")
            if pipeline_cfg:
                stages = pipeline_cfg.get("stages", [])
                active = [s for s in stages if s.get("status") != "skipped"]
                done = [s for s in active if s.get("status") in ("completed", "skipped")]
                total_active = len(active)
                done_count = len(done)
                current_idx = pipeline_cfg.get("current_stage_index", 0)
                current_stage_name = None
                if current_idx < len(stages):
                    current_stage_name = stages[current_idx].get("name")
                pct = round(done_count / total_active * 100) if total_active > 0 else 0
                item["pipeline_progress"] = f"{done_count}/{total_active}"
                item["pipeline_current_stage"] = current_stage_name
                item["pipeline_pct"] = pct

            # Nest subtasks into parent item.
            child_tasks = children_map.get(task.id, [])
            nested: list[dict] = []
            for child in child_tasks:
                stage_meta = subtask_id_to_stage.get(child.id, {})
                child_status = child.status if isinstance(child.status, str) else child.status.value
                nested.append({
                    "id": child.id,
                    "title": child.title,
                    "status": child_status,
                    "stage_name": stage_meta.get("name"),
                    "agent_template": stage_meta.get("agent_template"),
                    "completed_at": child.completed_at.isoformat() if child.completed_at else None,
                })
            item["subtasks"] = nested

            if h in wall:
                wall[h].append(item)

        # 每组内Sort by score descending
        for key in wall:
            wall[key].sort(key=lambda x: x["score"], reverse=True)

        # Sort completed tasks by completion time descending
        completed_tasks.sort(
            key=lambda x: x.get("completed_at") or "",
            reverse=True,
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
