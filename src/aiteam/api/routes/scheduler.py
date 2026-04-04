"""AI Team OS — Scheduler routes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])

MAX_TASKS_PER_TEAM = 20
MIN_INTERVAL_SECONDS = 300


class SchedulerCreateBody(BaseModel):
    name: str
    interval_seconds: int
    action_type: str
    action_config: dict[str, Any] = {}
    team_id: str | None = None
    description: str = ""


class SchedulerUpdateBody(BaseModel):
    enabled: bool | None = None
    name: str | None = None
    description: str | None = None
    interval_seconds: int | None = None
    action_type: str | None = None
    action_config: dict[str, Any] | None = None


@router.post("")
async def create_scheduled_task(
    body: SchedulerCreateBody,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Create a new scheduled task."""
    if body.interval_seconds < MIN_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"interval_seconds must be >= {MIN_INTERVAL_SECONDS} (5 minutes).",
        )

    valid_actions = ("create_task", "inject_reminder", "emit_event", "wake_agent")
    if body.action_type not in valid_actions:
        raise HTTPException(
            status_code=400,
            detail=f"action_type must be one of: {valid_actions}",
        )

    # Enforce max 20 tasks per team
    if body.team_id:
        existing = await repo.list_scheduled_tasks(team_id=body.team_id)
        if len(existing) >= MAX_TASKS_PER_TEAM:
            raise HTTPException(
                status_code=400,
                detail=f"Team already has {len(existing)} scheduled tasks (max {MAX_TASKS_PER_TEAM}).",
            )

    next_run_at = datetime.now() + timedelta(seconds=body.interval_seconds)
    task = await repo.create_scheduled_task(
        name=body.name,
        interval_seconds=body.interval_seconds,
        action_type=body.action_type,
        next_run_at=next_run_at,
        team_id=body.team_id,
        description=body.description,
        action_config=body.action_config,
    )
    return task.model_dump(mode="json")


@router.get("")
async def list_scheduled_tasks(
    team_id: str = "",
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List scheduled tasks."""
    tasks = await repo.list_scheduled_tasks(team_id=team_id if team_id else None)
    return {"items": [t.model_dump(mode="json") for t in tasks], "total": len(tasks)}


@router.get("/{task_id}")
async def get_scheduled_task(
    task_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Get a scheduled task by ID."""
    task = await repo.get_scheduled_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return task.model_dump(mode="json")


@router.put("/{task_id}")
async def update_scheduled_task(
    task_id: str,
    body: SchedulerUpdateBody,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Update a scheduled task (pause/resume/edit)."""
    existing = await repo.get_scheduled_task(task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")

    updates: dict[str, Any] = {}
    if body.enabled is not None:
        updates["enabled"] = body.enabled
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.interval_seconds is not None:
        if body.interval_seconds < MIN_INTERVAL_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"interval_seconds must be >= {MIN_INTERVAL_SECONDS}.",
            )
        updates["interval_seconds"] = body.interval_seconds
    if body.action_type is not None:
        updates["action_type"] = body.action_type
    if body.action_config is not None:
        updates["action_config"] = body.action_config

    updated = await repo.update_scheduled_task(task_id, **updates)
    return updated.model_dump(mode="json")  # type: ignore[union-attr]


@router.delete("/{task_id}")
async def delete_scheduled_task(
    task_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Delete a scheduled task permanently."""
    deleted = await repo.delete_scheduled_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return {"success": True, "task_id": task_id}


@router.put("/wake-pause-all")
async def pause_all_wake_agents(
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Emergency kill switch: disable all wake_agent scheduled tasks."""
    count = await repo.toggle_wake_agents(enabled=False)
    return {"paused": count, "message": f"Paused {count} wake_agent tasks"}


@router.put("/wake-resume-all")
async def resume_all_wake_agents(
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Resume all wake_agent scheduled tasks."""
    count = await repo.toggle_wake_agents(enabled=True)
    return {"resumed": count, "message": f"Resumed {count} wake_agent tasks"}
