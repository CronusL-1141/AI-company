"""AI Team OS — Project management + phase management routes."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException

from aiteam.api import session_probe, worktree_probe
from aiteam.api.deps import get_repository
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    PhaseCreate,
    PhaseStatusUpdate,
    ProjectCreate,
    ProjectUpdate,
)
from aiteam.clock import utc_now
from aiteam.loop.task_wall_engine import load_task_wall_digest
from aiteam.services.notices.detectors.registration import clear_registered
from aiteam.services.project_sessions import project_sessions
from aiteam.storage.repository import StorageRepository
from aiteam.types import Phase, PhaseStatus, Project, TaskStatus, TeamStatus

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
    # The folder (and every folder below it) is registered now: its
    # "not a registered project" notice is resolved.
    await clear_registered(repo, project.root_path)
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
    if "root_path" in updates:
        updates["root_path"] = await _checked_root_path(updates["root_path"], project_id, repo)
    project = await repo.update_project(project_id, **updates)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    if "root_path" in updates:
        await clear_registered(repo, project.root_path)
    return APIResponse(data=project, message="项目更新成功")


def _norm_dir(path: str) -> str:
    """Case-folded, separator-normalised form used only for comparisons."""
    return os.path.normpath(path).replace("\\", "/").rstrip("/").lower()


async def _checked_root_path(raw: str, project_id: str, repo: StorageRepository) -> str:
    """Validate a new project root and return it without trailing separators.

    A project root claims every directory beneath it by longest-prefix match
    (``deps._resolve_project_id_from_dir``). A path that does not exist never
    matches a session, and the home directory or any of its ancestors would
    claim every unregistered directory on the machine.
    """
    path = raw.strip()
    if not path or not os.path.isabs(path):
        raise HTTPException(status_code=400, detail=f"root_path 必须是绝对路径：'{raw}'")
    if not await asyncio.to_thread(os.path.isdir, path):
        raise HTTPException(status_code=400, detail=f"root_path 不存在或不是目录：'{raw}'")
    given = _norm_dir(path)
    home = _norm_dir(os.path.expanduser("~"))
    if home == given or home.startswith(given + "/"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"root_path '{raw}' 是家目录或其上级，范围过宽，不能作为项目根"
                "（会按前缀认领其下每个未注册目录）"
            ),
        )
    for other in await repo.list_projects():
        if other.id != project_id and other.root_path and _norm_dir(other.root_path) == given:
            raise HTTPException(
                status_code=409,
                detail=f"root_path '{raw}' 已是项目 {other.name}（{other.id}）的根目录",
            )
    return path.rstrip("/\\") or path


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


@router.get("/{project_id}/summary")
async def project_summary(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Quick project summary: status, active teams, top pending tasks.

    Task units differ by field: ``pending_tasks`` and ``running_tasks`` come from
    the task-wall digest and count top-level tasks only, while each session's
    ``in_flight_tasks`` and ``unattributed_running_tasks`` count every running
    task of the project, subtasks included.

    ``sessions`` keeps every live session plus the most recently active history
    up to ``MAX_SUMMARY_SESSIONS`` rows; ``session_count`` is the uncapped total
    and ``sessions_truncated`` says whether rows were left out.
    """
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")

    # Get all teams for this project
    teams = await repo.list_teams_by_project(project_id)
    active_teams = [t for t in teams if t.status == TeamStatus.ACTIVE]

    # Task counts and the top of the backlog come from the task-wall digest, the
    # same numbers the briefing and the task wall report (top-level tasks only).
    digest, _ = await load_task_wall_digest(repo, project_id)
    pending_count = digest.by_status.get(TaskStatus.PENDING.value, 0)
    running_count = digest.by_status.get(TaskStatus.RUNNING.value, 0)

    # Current busy cards and durable history share one read-only projection.
    # Freshness alone never proves that a host is working or has ended.
    probe_complete = True
    try:
        probes = await asyncio.to_thread(
            session_probe.detect_live_sessions, getattr(project, "root_path", "") or ""
        )
    except Exception:  # noqa: BLE001 - a failed observation is never an empty-history claim
        probes = []
        probe_complete = False
    session_summary = await project_sessions(
        repo, project_id, project.root_path or "", teams, probes, utc_now(),
    )
    session_summary["sessions_complete"] = session_summary["sessions_complete"] and probe_complete
    # Project activity and a currently busy host are different measurements:
    # recent transcript writes still mark activity even when host state is unknown.
    live_session = bool(session_summary["leaders"]) or any(probe.get("live") for probe in probes)
    is_active = len(active_teams) > 0 or running_count > 0 or live_session

    # Worktree 观测（docs/worktree-governance-design.md §4/(c)）：按需扫描，
    # 不做后台守护——每次 summary 请求触发一次只读 git 探测，与本函数其它探测段落
    # 同一原则（探测失败静默降级，绝不让 summary 整体报错）。
    try:
        worktrees = await asyncio.to_thread(
            worktree_probe.detect_worktrees, getattr(project, "root_path", "") or ""
        )
    except Exception:  # noqa: BLE001 — summary must not fail on this metric
        worktrees = []

    return {
        "status": "active" if is_active else "inactive",
        "active_teams": len(active_teams),
        "pending_tasks": pending_count,
        "running_tasks": running_count,
        **session_summary,
        "worktrees": worktrees,
        "top_tasks": [
            {"id": item.id, "title": item.title, "priority": item.priority, "horizon": item.horizon}
            for item in digest.top[:3]
        ],
    }


# ================================================================
# Phase management
# ================================================================


@router.post(
    "/{project_id}/phases",
    response_model=APIResponse[Phase],
    status_code=201,
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
            status_code=400,
            detail=f"无效状态 '{body.status}'，可选: {valid}",
        )

    # Get current phase
    phase = await repo.get_phase(phase_id)
    if phase is None:
        raise HTTPException(status_code=404, detail=f"阶段 {phase_id} 不存在")

    # Verify phase belongs to this project
    if phase.project_id != project_id:
        raise HTTPException(
            status_code=400,
            detail=f"阶段 {phase_id} 不属于项目 {project_id}",
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
