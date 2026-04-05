"""AI Team OS — Agent trust scoring API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_repository
from aiteam.loop.trust_scoring import get_agent_trust_scores, update_trust_score
from aiteam.storage.repository import StorageRepository

router = APIRouter(tags=["trust"])


@router.get("/api/agents/trust-scores")
async def list_trust_scores(
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Return all agents sorted by trust_score descending."""
    scores = await get_agent_trust_scores(repo)
    return {"data": scores, "total": len(scores)}


@router.post("/api/agents/{agent_id}/trust")
async def update_agent_trust(
    agent_id: str,
    task_result: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Update agent trust_score based on task outcome.

    Args:
        agent_id: Target agent ID
        task_result: One of "success", "failure", "timeout"
    """
    if task_result not in ("success", "failure", "timeout"):
        return {"error": f"Invalid task_result: {task_result!r}. Must be success/failure/timeout"}
    new_score = await update_trust_score(repo, agent_id, task_result)  # type: ignore[arg-type]
    if new_score < 0:
        return {"error": f"Agent {agent_id} not found"}
    return {"agent_id": agent_id, "task_result": task_result, "trust_score": new_score}
