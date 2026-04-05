"""Execution patterns API routes — record and search agent execution patterns."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.loop.execution_patterns import ExecutionPatternStore
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/execution-patterns", tags=["execution-patterns"])


@router.post("/record")
async def record_pattern(
    pattern_type: str,
    task_type: str,
    agent_template: str,
    approach: str,
    result: str = "",
    error: str = "",
    lesson: str = "",
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Record a success or failure execution pattern.

    Args:
        pattern_type: "success" or "failure"
        task_type: Task category (e.g. "api-implementation", "bug-fix")
        agent_template: Agent template name that executed the task
        approach: Description of the approach taken
        result: Result summary (for success patterns)
        error: Error description (for failure patterns)
        lesson: Lesson learned (for failure patterns)
    """
    store = ExecutionPatternStore(repo)
    if pattern_type == "success":
        memory_id = await store.record_success_pattern(
            task_type=task_type,
            agent_template=agent_template,
            approach=approach,
            result_summary=result,
        )
        return {"success": True, "memory_id": memory_id, "type": "success"}
    elif pattern_type == "failure":
        memory_id = await store.record_failure_pattern(
            task_type=task_type,
            agent_template=agent_template,
            approach=approach,
            error=error,
            lesson=lesson,
        )
        return {"success": True, "memory_id": memory_id, "type": "failure"}
    else:
        return {"success": False, "error": "pattern_type must be 'success' or 'failure'"}


@router.get("/search")
async def search_patterns(
    query: str = Query("", description="Task description to match against"),
    top_k: int = Query(3, description="Maximum number of results", ge=1, le=20),
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Search for similar historical execution patterns using BM25.

    Args:
        query: Task description to match against historical patterns
        top_k: Maximum number of patterns to return
    """
    store = ExecutionPatternStore(repo)
    patterns = await store.find_similar_patterns(query, top_k=top_k)
    return {"success": True, "patterns": patterns, "total": len(patterns)}
