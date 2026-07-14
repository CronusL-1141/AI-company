"""AI Team OS — Project management + phase management routes."""

from __future__ import annotations

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


@router.get("/{project_id}/summary")
async def project_summary(
    project_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Quick project summary: status, active teams, top pending tasks."""
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")

    # Get all teams for this project
    teams = await repo.list_teams_by_project(project_id)
    active_teams = [t for t in teams if t.status == TeamStatus.ACTIVE]

    # Get pending tasks
    pending_tasks = await repo.list_tasks_by_project(project_id, status=TaskStatus.PENDING)
    running_tasks = await repo.list_tasks_by_project(project_id, status=TaskStatus.RUNNING)

    # Live CC session: a leader agent bound to this project whose last_active_at
    # is fresh (hooks refresh it on every tool call) means someone is working in
    # this project right now, even with no running task on the wall.
    # Clock convention: agent timestamps are stored as NAIVE LOCAL time
    # (hook_translator/StateReaper both use datetime.now()); compare in the same
    # clock — treating them as UTC put liveness off by the UTC offset (4h observed).
    from datetime import datetime, timedelta

    live_session = False
    last_activity_at: str | None = None
    leader_info: dict | None = None
    leaders_info: list[dict] = []

    # Leader 身份 = 此项目目录下的 CC 主会话（文件真相源直读，零注册依赖）。
    # 用户裁定（2026-07-07）：模型/活跃状态由后端自动检测，不经 hook 注册链——
    # 注册链此前两度断裂（leader 行寄生 workflow 队被跨项目迁走、compact 合成行污染）。
    # 用户裁定（2026-07-10）：多会话并行时逐个展示为 CEO-<英文名>，不再只出最新一个。
    for probe in session_probe.detect_live_sessions(
        getattr(project, "root_path", "") or ""
    ):
        # 在飞任务（fleet 层 P2 观测，见 docs/fleet-layer-design.md §6.1）：
        # 本 session 名下 agent（leader + 其派出的子 agent）所属团队的 running 任务数。
        # 无 owner_session_id（那是 fleet P1 的地基，本批次未做）时的最佳努力口径——
        # 探测失败或该 session 尚无任何 agent 落库，均如实记 0，不猜测、不报错中断。
        in_flight_tasks = 0
        try:
            session_agents = await repo.find_agents_by_session(probe["session_id"])
            team_ids = {a.team_id for a in session_agents if a.team_id}
            for tid in team_ids:
                running = await repo.list_tasks(tid, status=TaskStatus.RUNNING)
                in_flight_tasks += len(running)
        except Exception:  # noqa: BLE001 — summary must not fail on this metric
            in_flight_tasks = 0

        leaders_info.append({
            "name": f"CEO-{probe['name']}",
            "model": probe["model"],
            "status": "busy" if probe["live"] else "offline",
            "session_id": probe["session_id"],
            "current_task": "",
            "last_active_at": probe["last_active_at"],
            "live": bool(probe["live"]),
            "ctx_tokens": probe.get("ctx_tokens"),
            "ctx_window": probe.get("ctx_window"),
            "ctx_pct": probe.get("ctx_pct"),
            "in_flight_tasks": in_flight_tasks,
        })
    if leaders_info:
        live_session = any(li["live"] for li in leaders_info)
        last_activity_at = leaders_info[0]["last_active_at"]
        leader_info = leaders_info[0]

    # DB leader 行仅作补充（current_task 等 hook 链才有的字段）与探测不可用时的兜底。
    try:
        leaders = await repo.find_agents_by_role("leader")
        now = datetime.now()
        freshest = None
        freshest_leader = None
        for leader in leaders:
            if getattr(leader, "project_id", None) != project_id:
                continue
            ts = getattr(leader, "last_active_at", None)
            if ts is None:
                continue
            if ts.tzinfo is not None:
                ts = ts.astimezone().replace(tzinfo=None)
            if freshest is None or ts > freshest:
                freshest = ts
                freshest_leader = leader
        if freshest_leader is not None:
            if leader_info is None:
                # Naive local, no timezone suffix — JS Date() parses it as local,
                # which matches how it was written.
                last_activity_at = freshest.isoformat() if freshest else None
                live_session = bool(
                    freshest and (now - freshest) < timedelta(minutes=15)
                )
                db_in_flight = 0
                try:
                    if getattr(freshest_leader, "team_id", None):
                        running = await repo.list_tasks(
                            freshest_leader.team_id, status=TaskStatus.RUNNING
                        )
                        db_in_flight = len(running)
                except Exception:  # noqa: BLE001 — summary must not fail on this metric
                    db_in_flight = 0
                leader_info = {
                    "name": freshest_leader.name,
                    "model": getattr(freshest_leader, "model", "") or "",
                    "status": str(getattr(freshest_leader, "status", "")),
                    "session_id": getattr(freshest_leader, "session_id", "") or "",
                    "current_task": getattr(freshest_leader, "current_task", "")
                    or "",
                    "last_active_at": last_activity_at,
                    "live": live_session,
                    # 无文件探测数据时（DB 兜底路径），水位未知，如实留空——
                    # 绝不把 agents 表的子 agent 水位口径误套到主会话行上。
                    "ctx_tokens": None,
                    "ctx_window": None,
                    "ctx_pct": None,
                    "in_flight_tasks": db_in_flight,
                }
                leaders_info.append(leader_info)
            else:
                # current_task 只有 hook 链才有——按 session_id 补给对应会话条目
                db_sid = getattr(freshest_leader, "session_id", "")
                for li in leaders_info:
                    if li["session_id"] == db_sid:
                        li["current_task"] = (
                            getattr(freshest_leader, "current_task", "") or ""
                        )
    except Exception:  # noqa: BLE001 — summary must not fail on liveness probe
        pass

    # Determine project status: active only if work is actively in progress
    # (any team active, any task running, or a live CC session in this project).
    # Pending backlog alone doesn't count — every project with unfinished tasks
    # would otherwise be "active" forever.
    is_active = len(active_teams) > 0 or len(running_tasks) > 0 or live_session

    # Top 3 pending tasks sorted by priority
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    top_tasks = sorted(
        pending_tasks,
        key=lambda t: priority_order.get(str(t.priority), 99),
    )[:3]

    # 该项目下出现过的去重 CC 会话数（agents.session_id 足迹）
    try:
        session_count = await repo.count_project_sessions(project_id)
    except Exception:  # noqa: BLE001 — summary must not fail on this metric
        session_count = 0

    # Worktree 观测（docs/worktree-governance-design.md §4/(c)）：按需扫描，
    # 不做后台守护——每次 summary 请求触发一次只读 git 探测，与本函数其它探测段落
    # 同一原则（探测失败静默降级，绝不让 summary 整体报错）。
    try:
        worktrees = worktree_probe.detect_worktrees(getattr(project, "root_path", "") or "")
    except Exception:  # noqa: BLE001 — summary must not fail on this metric
        worktrees = []

    return {
        "status": "active" if is_active else "inactive",
        "active_teams": len(active_teams),
        "pending_tasks": len(pending_tasks),
        "running_tasks": len(running_tasks),
        "session_count": session_count,
        "last_activity_at": last_activity_at,
        "leader": leader_info,
        "leaders": leaders_info,
        "worktrees": worktrees,
        "top_tasks": [
            {"title": t.title, "priority": str(t.priority)}
            for t in top_tasks
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
