"""AI Team OS — Project management + phase management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from aiteam.api.deps import get_repository
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    PhaseCreate,
    PhaseStatusUpdate,
    ProjectCreate,
    ProjectUpdate,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import Phase, PhaseStatus, Project

router = APIRouter(prefix="/api/projects", tags=["projects"])

# ================================================================
# Project CRUD
# ================================================================


@router.post("", response_model=APIResponse[Project], status_code=201)
async def create_project(
    body: ProjectCreate,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Project]:
    """Create a project with an automatically created default Phase."""
    project = await repo.create_project(
        name=body.name,
        root_path=body.root_path,
        description=body.description,
        config=body.config,
    )
    # Auto-create default Phase
    await repo.create_phase(
        project_id=project.id,
        name="Phase 1",
        description="Default initial phase",
        order=0,
    )
    return APIResponse(data=project, message="项目创建成功")


@router.get("", response_model=APIListResponse[Project])
async def list_projects(
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Project]:
    """List all projects."""
    projects = await repo.list_projects()
    return APIListResponse(data=projects, total=len(projects))


@router.get("/{project_id}", response_model=APIResponse[dict])
async def get_project(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[dict]:
    """Get project details, including phases list."""
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    phases = await repo.list_phases(project_id)
    data = project.model_dump()
    data["phases"] = [p.model_dump() for p in phases]
    return APIResponse(data=data, message="")


@router.put("/{project_id}", response_model=APIResponse[Project])
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Project]:
    """Update a project."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="无更新字段")
    project = await repo.update_project(project_id, **updates)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    return APIResponse(data=project, message="项目更新成功")


@router.delete("/{project_id}", response_model=APIResponse[bool])
async def delete_project(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[bool]:
    """Delete a project."""
    result = await repo.delete_project(project_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    return APIResponse(data=True, message="项目删除成功")


# ================================================================
# Phase management
# ================================================================


@router.post(
    "/{project_id}/phases", response_model=APIResponse[Phase], status_code=201,
)
async def create_phase(
    project_id: str,
    body: PhaseCreate,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Phase]:
    """Create a phase."""
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    phase = await repo.create_phase(
        project_id=project_id,
        name=body.name,
        description=body.description,
        order=body.order,
        config=body.config,
    )
    return APIResponse(data=phase, message="阶段创建成功")


@router.get("/{project_id}/phases", response_model=APIListResponse[Phase])
async def list_phases(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Phase]:
    """List all phases under a project."""
    phases = await repo.list_phases(project_id)
    return APIListResponse(data=phases, total=len(phases))


# Valid status transitions
_VALID_TRANSITIONS: dict[PhaseStatus, set[PhaseStatus]] = {
    PhaseStatus.PLANNING: {PhaseStatus.ACTIVE, PhaseStatus.ARCHIVED},
    PhaseStatus.ACTIVE: {PhaseStatus.COMPLETED, PhaseStatus.ARCHIVED},
    PhaseStatus.COMPLETED: {PhaseStatus.ARCHIVED, PhaseStatus.ACTIVE},
    PhaseStatus.ARCHIVED: set(),
}


@router.put(
    "/{project_id}/phases/{phase_id}/status",
    response_model=APIResponse[Phase],
)
async def update_phase_status(
    project_id: str,
    phase_id: str,
    body: PhaseStatusUpdate,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Phase]:
    """Update phase status with transition validation.

    Constraint: only one Phase can be active at a time within a project.
    """
    # Validate target status
    try:
        target_status = PhaseStatus(body.status)
    except ValueError:
        valid = [s.value for s in PhaseStatus]
        raise HTTPException(
            status_code=400, detail=f"无效状态 '{body.status}'，可选: {valid}",
        )

    # Get current phase
    phase = await repo.get_phase(phase_id)
    if phase is None:
        raise HTTPException(status_code=404, detail=f"阶段 {phase_id} 不存在")

    # Verify phase belongs to this project
    if phase.project_id != project_id:
        raise HTTPException(
            status_code=400, detail=f"阶段 {phase_id} 不属于项目 {project_id}",
        )

    # Check status transition validity
    current_status = phase.status
    allowed = _VALID_TRANSITIONS.get(current_status, set())
    if target_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"不允许从 {current_status.value} 转为 {target_status.value}，"
            f"允许: {[s.value for s in allowed]}",
        )

    # If target is active, first set other active phases in the project to completed
    if target_status == PhaseStatus.ACTIVE:
        deactivated = await repo.deactivate_phases(project_id)
        if deactivated > 0:
            msg = f"已将 {deactivated} 个旧 active 阶段设为 completed"
        else:
            msg = ""
    else:
        msg = ""

    updated = await repo.update_phase(phase_id, status=target_status)
    if updated is None:
        raise HTTPException(status_code=500, detail="更新失败")

    message = f"阶段状态更新为 {target_status.value}"
    if msg:
        message += f"（{msg}）"
    return APIResponse(data=updated, message=message)
