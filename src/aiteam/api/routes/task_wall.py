"""AI Team OS — 任务墙路由."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_loop_engine, get_repository
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
    """获取单团队任务墙视图.

    直接返回 {wall, stats} 结构，与前端 TaskWallResponse 类型对齐。
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
    """获取项目级任务墙视图 — 聚合该项目下所有团队的任务.

    直接返回 {wall, completed, stats} 结构，与前端 TaskWallResponse 类型对齐。
    """
    teams = await repo.list_teams_by_project(project_id)
    team_name_map: dict[str, str] = {t.id: t.name for t in teams}

    now = datetime.now()
    wall: dict[str, list[dict]] = {"short": [], "mid": [], "long": []}
    completed_tasks: list[dict] = []
    all_tasks_count = 0
    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    scores: list[float] = []

    for team in teams:
        tasks = await repo.list_tasks(team.id)
        all_tasks_count += len(tasks)

        for task in tasks:
            s = task.status if isinstance(task.status, str) else task.status.value
            by_status[s] = by_status.get(s, 0) + 1

            p = task.priority if isinstance(task.priority, str) else task.priority.value
            by_priority[p] = by_priority.get(p, 0) + 1

            item = task.model_dump(mode="json")
            item["team_name"] = team_name_map.get(task.team_id, "")

            if s == "completed":
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
