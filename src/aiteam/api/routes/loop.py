"""AI Team OS — Company loop routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aiteam.api.deps import get_loop_engine, get_repository
from aiteam.loop.engine import LoopEngine
from aiteam.loop.watchdog import WatchdogChecker
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/teams/{team_id}/loop", tags=["loop"])


class AdvanceBody(BaseModel):
    """Advance phase request body."""

    trigger: str


class NextTaskBody(BaseModel):
    """Get next task request body."""

    agent_id: str | None = None


@router.post("/start")
async def start_loop(
    team_id: str,
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """Start company loop."""
    state = await engine.start(team_id)
    return {
        "success": True,
        "data": state.model_dump(mode="json"),
        "message": f"循环已启动，当前周期: {state.current_cycle}",
    }


@router.get("/status")
async def get_loop_status(
    team_id: str,
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """Get loop status."""
    state = await engine.get_state(team_id)
    return {
        "success": True,
        "data": state.model_dump(mode="json"),
    }


@router.post("/next-task")
async def get_next_task(
    team_id: str,
    body: NextTaskBody | None = None,
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """Get the next task to execute."""
    agent_id = body.agent_id if body else None
    task = await engine.get_next_task(team_id, agent_id=agent_id)
    if task is None:
        return {
            "success": True,
            "data": None,
            "message": "当前没有待执行的任务",
        }
    return {
        "success": True,
        "data": task.model_dump(mode="json"),
    }


@router.post("/advance")
async def advance_loop(
    team_id: str,
    body: AdvanceBody,
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """Advance loop phase."""
    try:
        state = await engine.advance(team_id, body.trigger)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "success": True,
        "data": state.model_dump(mode="json"),
        "message": f"循环已推进到 {state.phase.value} 阶段",
    }


@router.post("/pause")
async def pause_loop(
    team_id: str,
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """Pause loop."""
    state = await engine.pause(team_id)
    return {
        "success": True,
        "data": state.model_dump(mode="json"),
        "message": "循环已暂停",
    }


@router.post("/resume")
async def resume_loop(
    team_id: str,
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """Resume loop."""
    state = await engine.resume(team_id)
    return {
        "success": True,
        "data": state.model_dump(mode="json"),
        "message": f"循环已恢复到 {state.phase.value} 阶段",
    }


@router.post("/review")
async def start_review(
    team_id: str,
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """Trigger review meeting and generate statistics report."""
    review = await engine.start_review(team_id)
    return {
        "success": True,
        "data": review,
        "message": f"回顾会议已创建: {review['topic']}",
    }


@router.post("/watchdog/check")
async def run_watchdog_check(
    team_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Run Watchdog checks and return alert list."""
    checker = WatchdogChecker(repo=repo)
    alerts = await checker.run_all_checks(team_id)
    return {
        "success": True,
        "data": alerts,
        "message": f"检查完成，发现 {len(alerts)} 个告警",
    }
