"""AI Team OS — 任务墙路由."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_loop_engine
from aiteam.loop.engine import LoopEngine

router = APIRouter(tags=["task-wall"])


@router.get("/api/teams/{team_id}/task-wall")
async def get_task_wall(
    team_id: str,
    horizon: str = "",
    priority: str = "",
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """获取任务墙视图."""
    wall = await engine.get_task_wall(team_id, horizon=horizon, priority=priority)
    return {
        "success": True,
        "data": wall,
    }
