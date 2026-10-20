"""Resolve short task IDs at the API boundary without broadening project scope."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException

from aiteam.api.deps import get_scoped_repository
from aiteam.storage.repository import StorageRepository


async def resolve_task_reference(task_id: str, repo: StorageRepository) -> str:
    """Full UUIDs retain exact lookup semantics; prefixes require a known project."""
    if len(task_id) == 36:
        try:
            if str(UUID(task_id)) == task_id.lower():
                return task_id
        except ValueError:
            pass
    # Repository validates the literal prefix as well, including wildcard input.
    shape = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    if not 8 <= len(task_id) < len(shape) or any(
        char != "-" if shape[index] == "-" else char not in "0123456789abcdefABCDEF"
        for index, char in enumerate(task_id)
    ):
        raise HTTPException(status_code=400, detail="任务 ID 前缀须为至少 8 位的 UUID 前缀")
    if not repo._project_scope or await repo.get_project(repo._project_scope) is None:
        raise HTTPException(status_code=400, detail="任务短 ID 需要明确且已注册的项目上下文")
    candidates = await repo.find_task_prefix_candidates(task_id)
    if not candidates:
        raise HTTPException(status_code=404, detail=f"当前项目中不存在任务 {task_id}")
    if len(candidates) > 1:
        raise HTTPException(status_code=409, detail={
            "error": "ambiguous_task_prefix", "prefix": task_id,
            "candidates": candidates[:20], "truncated": len(candidates) > 20,
        })
    return candidates[0]["id"]


async def resolve_task_id(
    task_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> str:
    return await resolve_task_reference(task_id, repo)


TaskId = Annotated[str, Depends(resolve_task_id)]
