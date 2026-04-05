"""AI Team OS — Watchdog heartbeat and health check routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_repository
from aiteam.loop.watchdog import agent_heartbeat, watchdog_check_heartbeats
from aiteam.storage.repository import StorageRepository

router = APIRouter(tags=["watchdog"])


@router.post("/api/watchdog/heartbeat")
async def record_heartbeat(body: dict) -> dict:
    """Record an agent heartbeat.

    Body fields:
        agent_id: str (required)
        agent_name: str (optional)
        team_id: str (optional)
    """
    agent_id = body.get("agent_id", "")
    if not agent_id:
        return {"success": False, "error": "agent_id is required"}
    return agent_heartbeat(
        agent_id=agent_id,
        agent_name=body.get("agent_name", ""),
        team_id=body.get("team_id", ""),
    )


@router.get("/api/watchdog/heartbeats")
async def check_heartbeats() -> dict:
    """Check all agent heartbeats and return alive/dead status."""
    return watchdog_check_heartbeats()


@router.post("/api/watchdog/verify/{task_id}")
async def verify_task_completion(
    task_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Verify whether a task is truly complete (has memo + summary + completed status)."""
    from aiteam.loop.completion_verifier import verify_completion
    return await verify_completion(task_id, repo)
