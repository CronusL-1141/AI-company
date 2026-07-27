"""AI Team OS — Watchdog health check routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_repository
from aiteam.loop.watchdog import WatchdogChecker
from aiteam.storage.repository import StorageRepository

router = APIRouter(tags=["watchdog"])


@router.post("/api/teams/{team_id}/watchdog/check")
async def run_watchdog_check(
    team_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Run Watchdog checks for a team and return the alert list.

    On-demand entry point for WatchdogChecker (agent/task/system health).
    Moved here from the retired loop routes (2026-07-27 scheduler slim-down).
    """
    checker = WatchdogChecker(repo=repo)
    alerts = await checker.run_all_checks(team_id)
    return {
        "success": True,
        "data": alerts,
        "message": f"检查完成，发现 {len(alerts)} 个告警",
    }


@router.post("/api/watchdog/verify/{task_id}")
async def verify_task_completion(
    task_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Verify whether a task is truly complete (has memo + summary + completed status)."""
    from aiteam.loop.completion_verifier import verify_completion
    return await verify_completion(task_id, repo)
