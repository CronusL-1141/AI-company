"""AI Team OS — Context resolution routes (cwd -> project_id)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from aiteam.api.deps import get_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/context", tags=["context"])


class ContextResolveRequest(BaseModel):
    cwd: str
    # 归属铁律（用户裁定 2026-07-08）：以 session 启动目录为准，匹配不到已注册
    # 项目就留空，绝不自动立项——默认 True 时代 hook 每次工具调用都可能把
    # 产出物目录（如 明日会议材料/demo）注册成项目。显式传 True 仍可注册。
    auto_create: bool = False


class ContextResolveResponse(BaseModel):
    project_id: str
    project_name: str
    root_path: str
    created: bool


def _normalize_path(p: str) -> str:
    try:
        return str(Path(p).resolve()).replace("\\", "/").rstrip("/")
    except Exception:
        return p.replace("\\", "/").rstrip("/")


@router.post("/resolve", response_model=ContextResolveResponse)
async def resolve_context(
    request: ContextResolveRequest,
    repo: StorageRepository = Depends(get_repository),
) -> ContextResolveResponse:
    """Resolve cwd to a project. Auto-creates project if no match and auto_create=True.

    Match rules (in order):
    1. Exact root_path match (case-insensitive on Windows)
    2. cwd is a subdirectory of any project's root_path (longest prefix wins)
    3. If no match and auto_create=True, create new project using directory basename
    """
    cwd_norm = _normalize_path(request.cwd)

    all_projects = await repo.list_projects()

    # Exact match
    for p in all_projects:
        if _normalize_path(p.root_path).lower() == cwd_norm.lower():
            return ContextResolveResponse(
                project_id=p.id,
                project_name=p.name,
                root_path=p.root_path,
                created=False,
            )

    # Prefix match — cwd is inside a project directory; longest prefix wins
    best_match = None
    best_len = 0
    for p in all_projects:
        rp_norm = _normalize_path(p.root_path)
        if not rp_norm:
            continue
        if cwd_norm.lower().startswith(rp_norm.lower() + "/") and len(rp_norm) > best_len:
            best_match = p
            best_len = len(rp_norm)

    if best_match is not None:
        return ContextResolveResponse(
            project_id=best_match.id,
            project_name=best_match.name,
            root_path=best_match.root_path,
            created=False,
        )

    if not request.auto_create:
        return ContextResolveResponse(
            project_id="",
            project_name="",
            root_path="",
            created=False,
        )

    # Auto-create: derive name from directory basename
    name = Path(cwd_norm).name or "project"
    created_project = await repo.create_project(
        name=name,
        root_path=cwd_norm,
        description=f"Auto-registered from cwd: {cwd_norm}",
        config={"auto_registered": True},
    )

    return ContextResolveResponse(
        project_id=created_project.id,
        project_name=created_project.name,
        root_path=created_project.root_path,
        created=True,
    )
