"""AI Team OS — Pipeline management routes."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.loop.pipeline import PIPELINE_TEMPLATES, SHORTCUT_PIPELINES, PipelineManager
from aiteam.storage.repository import StorageRepository

router = APIRouter(tags=["pipeline"])


# ============================================================
# Phase 1.3 — new stateful pipeline routes (/v2)
# ============================================================


@router.post("/api/tasks/{task_id}/pipeline/v2")
async def pipeline_v2_create(
    task_id: str,
    body: dict[str, Any],
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Create a pipeline using the new stateful lifecycle template design.

    Writes PipelineState into task.config['pipeline'] and records the initial
    stage_history entry.  Does NOT generate ceremonial subtasks.
    """
    from aiteam.pipeline.class_map import get_stage_class
    from aiteam.pipeline.clock import WallClock
    from aiteam.pipeline.templates import get_template

    task_type = body.get("task_type", "")
    if not task_type:
        return {"success": False, "error": "缺少 task_type 参数"}

    try:
        stages = get_template(task_type)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    # Check task exists
    task = await repo.get_task(task_id)
    if task is None:
        return {"success": False, "error": f"任务 {task_id} 不存在"}

    # Guard: reject if pipeline already initialised
    existing = await repo.get_pipeline_state(task_id)
    if existing is not None and existing.current_stage is not None:
        return {"success": False, "error": f"任务 {task_id} 已有 pipeline（当前阶段: {existing.current_stage}）"}

    clock = WallClock()
    first_stage = stages[0]
    first_class = get_stage_class(first_stage)

    await repo.set_pipeline_state(
        task_id,
        clock=clock,
        template=task_type,
        current_stage=first_stage,
        current_stage_class=first_class,
        autopilot_active=False,
        stage_started_at=True,  # sentinel: resolved to clock.now() inside repo
    )

    await repo.append_stage_history(
        task_id,
        from_stage=None,
        to_stage=first_stage,
        triggered_by="system",
        reason="pipeline_create",
        clock=clock,
    )

    state = await repo.get_pipeline_state(task_id)
    return {
        "success": True,
        "data": {
            "task_id": task_id,
            "template": task_type,
            "stages": stages,
            "current_stage": first_stage,
            "current_stage_class": first_class,
            "autopilot_active": False,
            "stage_started_at": state.stage_started_at.isoformat() if state and state.stage_started_at else None,
        },
    }


async def _check_exit_condition(
    task_id: str,
    current_stage: str,
    template: str,
    stage_started_at: datetime | None,
    project_root: str,
    repo: StorageRepository,
) -> bool:
    """Phase 2: delegate to fact-stream evaluator.

    Returns True only when AdvanceDecision.ADVANCE is returned.
    SUGGEST / NO_DECISION / FALL_BACK all block force=False advances.
    """
    from datetime import datetime

    from aiteam.pipeline.clock import WallClock
    from aiteam.pipeline.evaluator import AdvanceDecision, evaluate
    from aiteam.pipeline.fact_provider_db import DbFactProvider

    if stage_started_at is None:
        stage_started_at = datetime.now(tz=UTC)

    clock = WallClock()
    fact_provider = DbFactProvider(repo=repo, project_root=project_root)
    decision, _target, _reason = await evaluate(
        task_id=task_id,
        current_stage=current_stage,
        template=template,
        fact_provider=fact_provider,
        clock=clock,
        stage_started_at=stage_started_at,
    )
    return decision == AdvanceDecision.ADVANCE


@router.post("/api/tasks/{task_id}/pipeline/v2/advance")
async def pipeline_v2_advance(
    task_id: str,
    body: dict[str, Any] | None = None,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Advance a pipeline to the next (or specified) stage.

    Body (optional):
        target_stage: explicit target; omit to auto-select next in template sequence
        force: skip exit condition checks (default false)
        triggered_by: "manual" | "auto" | "force" | "system" (default "manual")
    """
    from aiteam.pipeline.class_map import get_stage_class
    from aiteam.pipeline.clock import WallClock
    from aiteam.pipeline.templates import get_next_stage

    body = body or {}
    target_stage: str | None = body.get("target_stage")
    force: bool = bool(body.get("force", False))
    triggered_by: str = body.get("triggered_by", "manual")

    task = await repo.get_task(task_id)
    if task is None:
        return {"success": False, "error": f"任务 {task_id} 不存在"}

    state = await repo.get_pipeline_state(task_id)
    if state is None or state.current_stage is None:
        return {"success": False, "error": f"任务 {task_id} 尚未初始化 pipeline，请先调用 pipeline_create"}

    current_stage = state.current_stage
    template = state.template or ""

    # Determine target
    if target_stage is None:
        target_stage = get_next_stage(template, current_stage)
        if target_stage is None:
            return {
                "success": False,
                "error": f"任务 {task_id} 当前阶段 '{current_stage}' 已是 '{template}' 模板的最后阶段，pipeline 已完成",
                "pipeline_completed": True,
            }

    # Exit condition gate (Phase 2: fact-stream evaluator)
    project_root = os.environ.get("AITEAM_PROJECT_ROOT", os.getcwd())
    if not force and not await _check_exit_condition(
        task_id=task_id,
        current_stage=current_stage,
        template=template,
        stage_started_at=state.stage_started_at,
        project_root=project_root,
        repo=repo,
    ):
        return {
            "success": False,
            "error": f"阶段 '{current_stage}' 尚未满足出口条件，使用 force=True 强制跳过",
        }

    clock = WallClock()
    new_class = get_stage_class(target_stage)

    await repo.set_pipeline_state(
        task_id,
        clock=clock,
        current_stage=target_stage,
        current_stage_class=new_class,
        stage_started_at=True,
    )

    await repo.append_stage_history(
        task_id,
        from_stage=current_stage,
        to_stage=target_stage,
        triggered_by=triggered_by,
        reason=f"advanced from {current_stage}",
        clock=clock,
    )

    state = await repo.get_pipeline_state(task_id)
    return {
        "success": True,
        "data": {
            "task_id": task_id,
            "from_stage": current_stage,
            "to_stage": target_stage,
            "current_stage_class": new_class,
            "template": template,
            "triggered_by": triggered_by,
            "force": force,
            "stage_started_at": state.stage_started_at.isoformat() if state and state.stage_started_at else None,
        },
    }


@router.get("/api/tasks/{task_id}/pipeline/v2")
async def pipeline_v2_status(
    task_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return current PipelineState and the 5 most recent stage_history entries."""
    from aiteam.pipeline.templates import LIFECYCLE_TEMPLATES

    task = await repo.get_task(task_id)
    if task is None:
        return {"success": False, "error": f"任务 {task_id} 不存在"}

    state = await repo.get_pipeline_state(task_id)
    if state is None or state.current_stage is None:
        return {"success": False, "error": f"任务 {task_id} 没有 pipeline"}

    history = await repo.read_stage_history(task_id, limit=5)
    history_data = [
        {
            "from_stage": t.from_stage,
            "to_stage": t.to_stage,
            "triggered_by": t.triggered_by,
            "reason": t.reason,
            "transitioned_at": t.transitioned_at.isoformat(),
        }
        for t in history
    ]

    template_stages = LIFECYCLE_TEMPLATES.get(state.template or "", [])

    return {
        "success": True,
        "data": {
            "task_id": task_id,
            "template": state.template,
            "stages": template_stages,
            "current_stage": state.current_stage,
            "current_stage_class": state.current_stage_class,
            "autopilot_active": state.autopilot_active,
            "stage_started_at": state.stage_started_at.isoformat() if state.stage_started_at else None,
            "recent_history": history_data,
        },
    }


@router.post("/api/tasks/{task_id}/pipeline/v2/autopilot")
async def pipeline_v2_autopilot(
    task_id: str,
    body: dict[str, Any] | None = None,
    repo: StorageRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Set autopilot_active flag on a task's pipeline.

    Body:
        active: true to enable, false to disable (required)
        max_duration_minutes: optional int, only used when active=true
    """
    from aiteam.pipeline.clock import WallClock

    body = body or {}
    if "active" not in body:
        return {"success": False, "error": "缺少 active 参数"}

    active: bool = bool(body["active"])
    max_duration_minutes: int | None = body.get("max_duration_minutes")

    task = await repo.get_task(task_id)
    if task is None:
        return {"success": False, "error": f"任务 {task_id} 不存在"}

    state = await repo.get_pipeline_state(task_id)
    if state is None or state.current_stage is None:
        return {"success": False, "error": f"任务 {task_id} 尚未初始化 pipeline"}

    clock = WallClock()
    updates: dict[str, Any] = {"autopilot_active": active}

    if active:
        updates["autopilot_started_at"] = clock.now().isoformat()
        if max_duration_minutes is not None:
            updates["autopilot_max_duration_minutes"] = max_duration_minutes
    # When deactivating, preserve autopilot_started_at for history

    await repo.set_pipeline_state(task_id, clock=clock, **updates)

    state = await repo.get_pipeline_state(task_id)
    pipeline_config = {}
    task_refreshed = await repo.get_task(task_id)
    if task_refreshed:
        config = task_refreshed.config or {}
        pipeline_config = config.get("pipeline", {}) if isinstance(config, dict) else {}

    return {
        "success": True,
        "data": {
            "task_id": task_id,
            "autopilot_active": active,
            "autopilot_started_at": pipeline_config.get("autopilot_started_at"),
            "autopilot_max_duration_minutes": pipeline_config.get("autopilot_max_duration_minutes"),
            "current_stage": state.current_stage if state else None,
            "current_stage_class": state.current_stage_class if state else None,
        },
    }


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
