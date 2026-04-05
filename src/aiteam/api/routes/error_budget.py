"""AI Team OS — SRE Error Budget routes."""

from __future__ import annotations

from fastapi import APIRouter

from aiteam.loop.error_budget import get_error_budget, update_error_budget

router = APIRouter(tags=["error-budget"])


@router.get("/api/error-budget")
async def get_budget(team_id: str = "global") -> dict:
    """Get current error budget status for a team.

    Query params:
        team_id: Team ID (default "global")
    """
    return get_error_budget(team_id)


@router.post("/api/error-budget/update")
async def update_budget(body: dict) -> dict:
    """Update error budget with a task result.

    Body fields:
        team_id: str (required)
        task_success: bool (required) — True if task succeeded
    """
    team_id = body.get("team_id", "global")
    task_success = body.get("task_success")
    if task_success is None:
        return {"success": False, "error": "task_success (bool) is required"}
    return update_error_budget(team_id=team_id, task_success=bool(task_success))
