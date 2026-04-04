"""AI Team OS — Pipeline management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.loop.pipeline import PIPELINE_TEMPLATES, SHORTCUT_PIPELINES, PipelineManager
from aiteam.storage.repository import StorageRepository

router = APIRouter(tags=["pipeline"])


@router.post("/api/tasks/{task_id}/pipeline")
async def create_pipeline(
    task_id: str,
    body: dict[str, Any],
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Create a pipeline for a task.

    Body:
        pipeline_type: Pipeline type (feature/bugfix/research/refactor/quick-fix/spike/hotfix)
        skip_stages: List of stage names to skip (optional)
    """
    pipeline_type = body.get("pipeline_type", "")
    skip_stages = body.get("skip_stages", [])

    if not pipeline_type:
        return {"success": False, "error": "缺少 pipeline_type 参数"}

    mgr = PipelineManager(repo)
    return await mgr.create_pipeline(task_id, pipeline_type, skip_stages)


@router.post("/api/tasks/{task_id}/pipeline/advance")
async def advance_pipeline(
    task_id: str,
    body: dict[str, Any] | None = None,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Advance pipeline to the next stage (mark current stage completed).

    Body (optional):
        result_summary: Summary of what was accomplished
    """
    result_summary = ""
    if body:
        result_summary = body.get("result_summary", "")

    mgr = PipelineManager(repo)
    return await mgr.advance_stage(task_id, result_summary)


@router.post("/api/tasks/{task_id}/pipeline/fail")
async def fail_pipeline_stage(
    task_id: str,
    body: dict[str, Any] | None = None,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Mark current pipeline stage as failed (triggers rollback if applicable).

    Body (optional):
        reason: Failure reason
    """
    reason = ""
    if body:
        reason = body.get("reason", "")

    mgr = PipelineManager(repo)
    return await mgr.fail_stage(task_id, reason)


@router.post("/api/tasks/{task_id}/pipeline/skip")
async def skip_pipeline_stage(
    task_id: str,
    body: dict[str, Any],
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Skip a pipeline stage.

    Body:
        stage_name: Name of the stage to skip
    """
    stage_name = body.get("stage_name", "")
    if not stage_name:
        return {"success": False, "error": "缺少 stage_name 参数"}

    mgr = PipelineManager(repo)
    return await mgr.skip_stage(task_id, stage_name)


@router.get("/api/tasks/{task_id}/pipeline")
async def get_pipeline_status(
    task_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get pipeline progress overview."""
    mgr = PipelineManager(repo)
    return await mgr.get_pipeline_status(task_id)


@router.get("/api/pipeline/templates")
async def list_pipeline_templates() -> dict[str, Any]:
    """List all available pipeline templates."""
    all_templates: dict[str, Any] = {}
    for key, stages in PIPELINE_TEMPLATES.items():
        all_templates[key] = {
            "type": "standard",
            "stages": [s["name"] for s in stages],
            "stage_count": len(stages),
        }
    for key, stages in SHORTCUT_PIPELINES.items():
        all_templates[key] = {
            "type": "shortcut",
            "stages": [s["name"] for s in stages],
            "stage_count": len(stages),
        }
    return {"success": True, "data": all_templates, "total": len(all_templates)}
