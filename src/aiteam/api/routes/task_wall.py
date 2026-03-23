"""AI Team OS — Task wall routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aiteam.api.deps import get_loop_engine, get_repository
from aiteam.loop.auto_assign import TaskMatcher
from aiteam.loop.engine import LoopEngine, calculate_task_score
from aiteam.storage.repository import StorageRepository

router = APIRouter(tags=["task-wall"])


@router.get("/api/teams/{team_id}/task-wall")
async def get_task_wall(
    team_id: str,
    horizon: str = "",
    priority: str = "",
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """Get single-team task wall view.

    Returns {wall, stats} structure directly, aligned with frontend TaskWallResponse type.
    """
    result = await engine.get_task_wall(team_id, horizon=horizon, priority=priority)
    # engine.get_task_wall 返回 {"wall": {...}, "stats": {...}}
    return result


@router.get("/api/projects/{project_id}/task-wall")
async def get_project_task_wall(
    project_id: str,
    horizon: str = "",
    priority: str = "",
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Get project-level task wall view — query all tasks by project_id (including team_id=None project-level tasks).

    Returns {wall, completed, stats} structure directly, aligned with frontend TaskWallResponse type.
    """
    # Check if project exists
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    # Query all tasks directly by project_id, not by iterating teams
    all_project_tasks = await repo.list_tasks_by_project(project_id)

    # Build team_name mapping (for tasks with team_id)
    teams = await repo.list_teams_by_project(project_id)
    team_name_map: dict[str, str] = {t.id: t.name for t in teams}

    # Build parent_id → children mapping so subtasks can be nested into parent items.
    # Map subtask_id → stage definition for agent_template and stage_name lookup.
    subtask_id_to_stage: dict[str, dict] = {}
    children_map: dict[str, list] = {}
    for task in all_project_tasks:
        if task.parent_id:
            children_map.setdefault(task.parent_id, []).append(task)
            # Index by task id so we can look up stage metadata later.
            subtask_id_to_stage[task.id] = {}

    # Populate stage metadata from parent pipeline configs.
    for task in all_project_tasks:
        pipeline_cfg = task.config.get("pipeline")
        if not pipeline_cfg:
            continue
        for stage in pipeline_cfg.get("stages", []):
            sid = stage.get("subtask_id")
            if sid and sid in subtask_id_to_stage:
                subtask_id_to_stage[sid] = stage

    now = datetime.now()
    wall: dict[str, list[dict]] = {"short": [], "mid": [], "long": []}
    completed_tasks: list[dict] = []
    all_tasks_count = len(all_project_tasks)
    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    scores: list[float] = []

    for task in all_project_tasks:
        # Filter out pipeline subtasks — they should not appear as top-level wall cards.
        if task.parent_id:
            continue

        s = task.status if isinstance(task.status, str) else task.status.value
        by_status[s] = by_status.get(s, 0) + 1

        p = task.priority if isinstance(task.priority, str) else task.priority.value
        by_priority[p] = by_priority.get(p, 0) + 1

        item = task.model_dump(mode="json")
        item["team_name"] = team_name_map.get(task.team_id, "") if task.team_id else ""

        if s == "completed":
            # Nest subtasks for completed parent tasks as well.
            child_tasks = children_map.get(task.id, [])
            if child_tasks:
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
                item["subtasks"] = nested_c
            else:
                item["subtasks"] = []
            completed_tasks.append(item)
            continue

        h = task.horizon if isinstance(task.horizon, str) else task.horizon.value
        if horizon and h != horizon:
            continue
        if priority and p not in priority.split(","):
            continue

        score = calculate_task_score(task, now)
        item["score"] = round(score, 1)
        scores.append(score)

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

        # Nest subtasks into parent item so the frontend can display pipeline stages.
        child_tasks = children_map.get(task.id, [])
        if child_tasks:
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
        else:
            item["subtasks"] = []

        if h in wall:
            wall[h].append(item)

    # 每组内Sort by score descending
    for key in wall:
        wall[key].sort(key=lambda x: x["score"], reverse=True)

    # Completed tasks sorted by completion time descending
    completed_tasks.sort(
        key=lambda x: x.get("completed_at") or "",
        reverse=True,
    )

    stats = {
        "total": all_tasks_count,
        "by_status": by_status,
        "by_priority": by_priority,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "completed_count": len(completed_tasks),
    }

    return {
        "wall": wall,
        "completed": completed_tasks,
        "stats": stats,
    }


@router.get("/api/teams/{team_id}/task-matches")
async def get_task_matches(
    team_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Get task-Agent smart matching suggestions.

    Returns optimal match list of pending unassigned tasks with idle agents.
    Matching algorithm: keyword intersection scoring between Agent role and task tags.
    """
    matcher = TaskMatcher(repo)
    matches = await matcher.find_matches(team_id)
    return {
        "success": True,
        "data": matches,
        "total": len(matches),
    }
