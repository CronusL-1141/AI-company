"""AI Team OS — Memory query routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.api.schemas import (
    APIListResponse,
    MemoryCreate,
    MemoryInvalidate,
    MemoryInvalidateByMatch,
)
from aiteam.memory.content_safety import scan_direction_content
from aiteam.memory.scoping import dir_bucket_scope_id
from aiteam.storage.repository import StorageRepository
from aiteam.types import Memory

router = APIRouter(prefix="/api/memory", tags=["memory"])

# ================================================================
# 方向层记忆（记忆系统 v2 P1）— POST/GET/invalidate 三入口
# ================================================================

# 体量红线 —— 单一轴（v2.1，2026-07-31 裁定）：**存储上限 = 注入预算**。
#
# 旧口径是两根不同的轴：存储侧「每桶 ≤40 条 × 单条 ≤400 字」允许 16,000 字，
# 注入侧预算 900 字。差 17 倍的后果不是"存多了没用"，是"存了等于没存"——实测
# 48 条有效条目里只有头 2-3 条真的进了 agent 的 system prompt，其余被截成一句
# "另有 46 条"，方向层"派出的 agent 出生即继承"的卖点被自己的红线架空。
#
# 现在只剩一根轴：注入池 = global/system + 当前 project + user 三桶联合 3000 字，
# 各桶独立配额如下。容量压力被前移到写入那一刻（超限即拒 + 交回全桶清单要求先
# 整理），注入侧不再常态截断——**存得下就一定传得到**。
# 单条 400 字上限保留：指针条目哲学（触发条件 + 指向权威文件，正文外置）不变。
_MAX_CONTENT_CHARS = 400  # 单条内容字数上限
_BUCKET_QUOTA_CHARS = {"global": 1200, "project": 1500, "user": 300}
# 一个会话实际继承的注入池 = 三桶各一份（project 桶按项目各自计配额）。
_DIRECTION_TOTAL_BUDGET = sum(_BUCKET_QUOTA_CHARS.values())  # 3000

# scope→默认 scope_id（未显式给定时推导）
_DEFAULT_SCOPE_ID = {"global": "system", "user": "user"}

router_memories = APIRouter(prefix="/api/memories", tags=["memory"])


def _resolve_scope_id(scope: str, scope_id: str, repo: StorageRepository) -> str:
    """按 scope 推导 scope_id：project→当前项目/未注册目录临时桶、global→system、user→user。

    未注册目录（repo 无 _project_scope）下的 scope=project **不再静默回落 "system"**
    ——那等于把本目录的项目记忆广播成全局记忆（2026-07-21 串线事故根因）。改用由
    cwd（X-Project-Dir 头）派生的目录指纹临时桶 "dir:<sha1>"；连 cwd 都拿不到（无
    header 的裸调用）则抛 422 拒绝，提示带 X-Project-Dir 或改用 scope=global，绝不落 system。
    """
    if scope_id:
        return scope_id
    if scope == "project":
        if repo._project_scope:
            return repo._project_scope
        bucket = dir_bucket_scope_id(repo._unresolved_dir)
        if bucket:
            return bucket
        raise HTTPException(
            status_code=422,
            detail=(
                "scope=project 写入需要目录上下文：当前请求既未匹配到已注册项目，"
                "也缺少 cwd（无 X-Project-Dir 头）。请在带 X-Project-Dir 的会话中写入，"
                "或改用 scope=global（仅当这条对任意目录的任意会话都成立时）。"
            ),
        )
    return _DEFAULT_SCOPE_ID.get(scope, "system")


async def _bucket_quota_check(
    repo: StorageRepository,
    scope: str,
    scope_id: str,
    incoming_chars: int,
    replaced: Memory | None = None,
    *,
    include_entries: bool = True,
) -> dict | None:
    """按桶字符配额校验；放得下返回 None，放不下返回**超限协议**响应体。

    口径是「应用置换之后的桶总字符」：supersedes 会腾出旧条的字数，所以拿一条
    长条换一条短条永远不该被拒（无脑加新条字数会误拒，那是旧数量轴留下的思维）。

    超限协议（Hermes 机制照搬）：不是回一句"先整理再添加"就完事——把该桶**当前
    全部有效条目**（id / kind / 字数 / 全文）连同用量与缺口一起交回，并明确要求
    在本轮内先腾空间再重试本次写入。整理必须发生在被拒绝的那一刻，否则调用方
    只会换个短句子重试，容量压力照样没人处理。
    """
    quota = _BUCKET_QUOTA_CHARS.get(scope)
    if quota is None:  # 非方向层 scope 不受配额约束（team/agent 遗留分区）
        return None

    entries = await repo.list_memories(scope, scope_id)
    entries.sort(key=lambda m: m.created_at)
    used = sum(len(m.content) for m in entries)
    freed = len(replaced.content) if replaced is not None else 0
    projected = used - freed + incoming_chars
    if projected <= quota:
        return None

    over_by = projected - quota
    replace_note = f"（本次置换可腾出 {freed} 字）" if freed else ""
    where_entries = (
        "本响应的 bucket_entries 字段是该桶当前全部有效条目（含 id / kind / 字数 / 全文）："
        if include_entries
        else "该桶全部有效条目见本次整理返回的 direction_inventory（或调 memory_list）："
    )
    payload: dict = {
        "success": False,
        "error": (
            f"写入被拒：作用域 {scope}/{scope_id} 的方向层配额是 {quota} 字，"
            f"当前已用 {used} 字（{len(entries)} 条），本次新增 {incoming_chars} 字"
            f"{replace_note}，落库后将达 {projected} 字，超出 {over_by} 字。\n"
            "方向层的存储上限就是注入预算——存得下的才传得到，所以这里不能加塞。\n"
            f"{where_entries}请在**本轮之内**逐条判断哪些已陈旧或可合并，用 "
            "memory_invalidate（可传 content_match 子串定位）或 "
            f"memory_reconcile_apply 腾出至少 {over_by} 字，然后重试本次写入。"
        ),
        "quota": {
            "scope": scope,
            "scope_id": scope_id,
            "quota_chars": quota,
            "used_chars": used,
            "entry_count": len(entries),
            "incoming_chars": incoming_chars,
            "freed_by_supersedes": freed,
            "projected_chars": projected,
            "over_by_chars": over_by,
            "total_injection_budget": _DIRECTION_TOTAL_BUDGET,
        },
        "next_action": (
            f"先失效/合并该桶中至少 {over_by} 字的陈旧条目，再重试本次写入。"
        ),
    }
    if include_entries:
        payload["bucket_entries"] = [
            {
                "id": m.id,
                "kind": m.kind,
                "chars": len(m.content),
                "created_at": m.created_at.isoformat(),
                "content": m.content,
            }
            for m in entries
        ]
    return payload


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

    # 写入安全扫描（v2.1）：方向层条目进每个派出 agent 的 system prompt，
    # memory_add 因此是注入放大器——不可见字符/提示注入句式/凭据形态在此拦下。
    finding = scan_direction_content(content)
    if finding is not None:
        return {
            "success": False,
            "error": finding.message,
            "safety": {"category": finding.category, "pattern": finding.pattern},
        }

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

    # supersedes 必须真实置换才算置换（审查 major：不存在/已失效/跨桶的
    # supersedes id 曾可绕过红线且不失效任何旧条 → 无限净增）。校验通过后，
    # 旧条的字数在配额口径里被计为"腾出"。
    replaced: Memory | None = None
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
        replaced = old

    # 体量红线②：桶字符配额（存储上限 = 注入预算）→ 超限走 Hermes 超限协议
    over_quota = await _bucket_quota_check(
        repo, body.scope, scope_id, len(content), replaced
    )
    if over_quota is not None:
        return over_quota

    memory = await repo.create_memory(
        scope=body.scope,
        scope_id=scope_id,
        content=content,
        kind=body.kind,
        source_refs=body.source_refs,
        supersedes=body.supersedes,
    )
    return {"success": True, "data": memory.model_dump(mode="json")}


@router_memories.post("/invalidate")
async def invalidate_direction_memory_by_match(
    body: MemoryInvalidateByMatch,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """按内容子串定位并失效一条方向层记忆（Hermes replace/remove 的定位协议）。

    整理常发生在「刚被超限协议顶回来」的那一刻，此时调用方手里有的是条目原文，
    先查一次 id 再失效纯属多一跳。子串必须**唯一**命中：命中 0 条或多条都不动
    数据，多条时把候选交回要求给出更精确的子串——绝不猜。

    检索面与 memory_list / 注入同源：global + user + 当前项目的 project 桶。
    """
    needle = (body.content_match or "").strip()
    if not needle:
        raise HTTPException(status_code=422, detail="content_match 不能为空")

    project_id = repo._project_scope or dir_bucket_scope_id(repo._unresolved_dir) or None
    candidates = await repo.list_direction_memories(project_id=project_id)
    matched = [m for m in candidates if needle in m.content]

    if not matched:
        return {
            "success": False,
            "error": (
                f"未匹配到包含子串「{needle}」的有效方向层条目。"
                "先用 memory_list 核对原文（子串区分大小写与标点）。"
            ),
        }
    if len(matched) > 1:
        return {
            "success": False,
            "error": (
                f"子串「{needle}」命中 {len(matched)} 条，无法确定要失效哪一条。"
                "请给出更长的唯一子串，或直接传 memory_id。"
            ),
            "matches": [
                {"id": m.id, "kind": m.kind, "excerpt": m.content[:80]} for m in matched
            ],
        }

    memory = await repo.invalidate_memory(
        matched[0].id, invalidated_by=body.invalidated_by
    )
    return {"success": True, "data": memory.model_dump(mode="json") if memory else None}


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
    的 project 级条目——双 hook 常驻注入的数据源。未注册目录（无 _project_scope
    但带 X-Project-Dir）读回的是本目录指纹临时桶 "dir:<sha1>"，与写路径对称：
    存自己的、继承自己的，读不到其他目录的临时桶（2026-07-21 串线事故根治）。
    """
    project_id = repo._project_scope or dir_bucket_scope_id(repo._unresolved_dir) or None
    memories = await repo.list_direction_memories(
        project_id=project_id,
        kind=kind or None,
        include_invalidated=include_invalidated,
    )
    return APIListResponse(data=memories, total=len(memories))


@router.get("", response_model=APIListResponse[Memory])
async def search_memories(
    scope: str = Query("global", description="Memory scope"),
    scope_id: str = Query(
        "",
        description=(
            "Scope ID；留空则按 scope 推导（global→system、user→user、"
            "project→当前项目 id 或未注册目录指纹临时桶）"
        ),
    ),
    query: str = Query("", description="Search keywords"),
    limit: int = Query(10, ge=1, le=100, description="Return count limit"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> APIListResponse[Memory]:
    """Search memories（scope_id 缺省时按上下文推导，与 list/写路径同规）。

    2026-07-21 事故 follow-up：本端点原用非 scoped repo + 显式 scope_id（默认 system），
    完全不走 X-Project-Dir 链路——未注册目录下 memory_search 查不到自己刚写的临时桶。
    现改依赖 get_scoped_repository：project 作用域下 scope_id 缺省（含旧 MCP 默认占位
    "system"）时，按 scoped repo 解析出当前项目 id 或未注册目录的指纹桶；连 cwd 都没有
    则 422。**显式传入的 scope_id 一律尊重**（M4 巡检等显式传参路径不受影响）。
    """
    effective_scope_id = scope_id
    if scope == "project" and effective_scope_id == "system":
        # project 下的 "system" 是旧 memory_search MCP 默认占位，无意义 → 视为未指定
        effective_scope_id = ""
    resolved_scope_id = _resolve_scope_id(scope, effective_scope_id, repo)
    if query:
        memories = await repo.search_memories(scope, resolved_scope_id, query, limit)
    else:
        memories = await repo.list_memories(scope, resolved_scope_id)
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
