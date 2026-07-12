"""AI Team OS — Memory query routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.api.schemas import APIListResponse, MemoryCreate, MemoryInvalidate
from aiteam.storage.repository import StorageRepository
from aiteam.types import Memory

router = APIRouter(prefix="/api/memory", tags=["memory"])

# ================================================================
# 方向层记忆（记忆系统 v2 P1）— POST/GET/invalidate 三入口
# ================================================================

# 体量红线（Letta block 字符上限的教训）：方向层价值在小而准，不在多。
_MAX_VALID_PER_BUCKET = 40  # 同 (scope, scope_id) 桶有效条目上限
_MAX_CONTENT_CHARS = 400  # 单条内容字数上限

# scope→默认 scope_id（未显式给定时推导）
_DEFAULT_SCOPE_ID = {"global": "system", "user": "user"}

router_memories = APIRouter(prefix="/api/memories", tags=["memory"])


def _resolve_scope_id(scope: str, scope_id: str, repo: StorageRepository) -> str:
    """按 scope 推导 scope_id：project→当前项目、global→system、user→user。"""
    if scope_id:
        return scope_id
    if scope == "project":
        return repo._project_scope or "system"
    return _DEFAULT_SCOPE_ID.get(scope, "system")


@router_memories.post("")
async def create_direction_memory(
    body: MemoryCreate,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """写一条方向层记忆（体量红线在此强制，超限拒绝并提示先整理）。"""
    if body.scope not in ("global", "project", "user"):
        raise HTTPException(
            status_code=422,
            detail=f"方向层 scope 只能是 global/project/user，收到 {body.scope!r}",
        )
    if body.kind not in repo.DIRECTION_KINDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"kind 只能是 {'/'.join(repo.DIRECTION_KINDS)}，收到 {body.kind!r}"
            ),
        )

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content 不能为空")

    # 体量红线①：单条 > 400 字 → 拒绝（超限内容应降级为「指针条目」，正文外置）
    if len(content) > _MAX_CONTENT_CHARS:
        return {
            "success": False,
            "error": (
                f"内容 {len(content)} 字超过方向层单条上限 {_MAX_CONTENT_CHARS} 字。"
                "方向层价值在小而准：请精简，或改为「触发条件 + 指向权威文件」的指针条目，"
                "大体量正文放情景层/报告由检索按需拉取。"
            ),
        }

    scope_id = _resolve_scope_id(body.scope, body.scope_id, repo)

    # supersedes 必须真实置换才能豁免数量红线（审查 major：不存在/已失效/
    # 跨桶的 supersedes id 曾可跳过 40 上限且不失效任何旧条 → 无限净增）
    if body.supersedes is not None:
        old = await repo.get_memory(body.supersedes)
        if old is None or old.invalid_at is not None:
            return {
                "success": False,
                "error": (
                    f"supersedes 指向的记忆 {body.supersedes} 不存在或已失效，"
                    "无法作为置换写入。如为新增请去掉 supersedes 参数。"
                ),
            }
        if old.scope.value != body.scope or old.scope_id != scope_id:
            return {
                "success": False,
                "error": (
                    f"supersedes 目标属于 {old.scope.value}/{old.scope_id}，"
                    f"与本条 {body.scope}/{scope_id} 不同桶，禁止跨桶置换。"
                ),
            }

    # 体量红线②：同桶有效条目 ≥ 40 → 拒绝（先整理再添加）
    valid_count = await repo.count_valid_memories(body.scope, scope_id)
    if body.supersedes is None and valid_count >= _MAX_VALID_PER_BUCKET:
        return {
            "success": False,
            "error": (
                f"该作用域（{body.scope}/{scope_id}）已有 {valid_count} 条有效方向记忆，"
                f"达上限 {_MAX_VALID_PER_BUCKET} 条。先用 memory_reconcile 整理"
                "（合并/失效冗余条目）再添加——方向层的价值在小而准，不在多。"
            ),
        }

    memory = await repo.create_memory(
        scope=body.scope,
        scope_id=scope_id,
        content=content,
        kind=body.kind,
        source_refs=body.source_refs,
        supersedes=body.supersedes,
    )
    return {"success": True, "data": memory.model_dump(mode="json")}


@router_memories.post("/{memory_id}/invalidate")
async def invalidate_direction_memory(
    memory_id: str,
    body: MemoryInvalidate | None = None,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """显式失效一条方向层记忆（不删除，Zep 失效语义）。"""
    invalidated_by = body.invalidated_by if body else None
    memory = await repo.invalidate_memory(memory_id, invalidated_by=invalidated_by)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"记忆 {memory_id} 不存在")
    return {"success": True, "data": memory.model_dump(mode="json")}


@router_memories.get("", response_model=APIListResponse[Memory])
async def list_direction_memories(
    kind: str = Query("", description="按 kind 过滤：constraint/design/directive/preference"),
    include_invalidated: bool = Query(False, description="是否含已失效条目"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> APIListResponse[Memory]:
    """列方向层有效条目（valid-only 默认），按 kind 优先级 + 时间倒序。

    自动纳入 global + user 全局条目，及当前项目（X-Project-Id / X-Project-Dir）
    的 project 级条目——双 hook 常驻注入的数据源。
    """
    memories = await repo.list_direction_memories(
        project_id=repo._project_scope or None,
        kind=kind or None,
        include_invalidated=include_invalidated,
    )
    return APIListResponse(data=memories, total=len(memories))


@router.get("", response_model=APIListResponse[Memory])
async def search_memories(
    scope: str = Query("global", description="Memory scope"),
    scope_id: str = Query("system", description="Scope ID"),
    query: str = Query("", description="Search keywords"),
    limit: int = Query(10, ge=1, le=100, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """Search memories."""
    if query:
        memories = await repo.search_memories(scope, scope_id, query, limit)
    else:
        memories = await repo.list_memories(scope, scope_id)
        memories = memories[:limit]
    return APIListResponse(data=memories, total=len(memories))


# ================================================================
# Team knowledge base endpoint
# ================================================================

router_teams_memory = APIRouter(prefix="/api/teams", tags=["memory"])


@router_teams_memory.get("/{team_id}/knowledge", response_model=APIListResponse[Memory])
async def get_team_knowledge(
    team_id: str,
    type: str = Query(
        "", description="Type filter: failure_alchemy / lesson_learned / loop_review"
    ),
    limit: int = Query(50, ge=1, le=200, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """Get team knowledge base.

    Returns the team's scope=team memory list, including:
    - failure_alchemy generated failure lessons
    - lesson_learned manually recorded experiences
    - loop_review retrospective summaries
    Sorted by created_at descending, supports ?type= filtering.
    """
    memories = await repo.list_team_knowledge(
        team_id=team_id,
        memory_type=type or None,
        limit=limit,
    )
    return APIListResponse(data=memories, total=len(memories))


# ================================================================
# Agent experience summary endpoint
# ================================================================

router_agents_memory = APIRouter(prefix="/api/agents", tags=["memory"])


@router_agents_memory.get("/{agent_id}/experience", response_model=APIListResponse[Memory])
async def get_agent_experience(
    agent_id: str,
    limit: int = Query(50, ge=1, le=200, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Memory]:
    """Get Agent experience summary.

    Returns the Agent's scope=agent memory list,
    including task completion records and accumulated experience.
    """
    memories = await repo.list_agent_experience(agent_id=agent_id, limit=limit)
    return APIListResponse(data=memories, total=len(memories))
