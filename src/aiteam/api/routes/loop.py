"""AI Team OS — 公司循环路由."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aiteam.api.deps import get_loop_engine
from aiteam.loop.engine import LoopEngine

router = APIRouter(prefix="/api/teams/{team_id}/loop", tags=["loop"])


class AdvanceBody(BaseModel):
    """推进阶段请求体."""

    trigger: str


class NextTaskBody(BaseModel):
    """获取下一个任务请求体."""

    agent_id: str | None = None


@router.post("/start")
async def start_loop(
    team_id: str,
    engine: LoopEngine = Depends(get_loop_engine),
) -> dict[str, Any]:
    """启动公司循环."""
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
    """获取循环状态."""
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
    """获取下一个应执行的任务."""
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
    """推进循环阶段."""
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
    """暂停循环."""
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
    """恢复循环."""
    state = await engine.resume(team_id)
    return {
        "success": True,
        "data": state.model_dump(mode="json"),
        "message": f"循环已恢复到 {state.phase.value} 阶段",
    }
