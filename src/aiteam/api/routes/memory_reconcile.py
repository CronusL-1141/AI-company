"""AI Team OS — Memory reconcile 路由（记忆系统 v2 P2）.

设计 §4「按需整理」两端点：
- GET  /api/memory/reconcile/candidates —— 确定性粗筛（零 LLM）：情景层候选组
  （簇内 BM25 配对）+ 方向层清单（供逐条陈旧检查）+ 蒸馏素材（promotion 候选）+
  操作说明。判定交给调用工具的 CC 会话内 agent（OS 无独立 LLM 凭据）。
- POST /api/memory/reconcile/apply —— 批量应用 agent 确认后的操作
  （merge/invalidate/score/promote），幂等：对已失效条目重复操作返回 noop。

两道闸（2026-09-28，设计见 docs/memory-v2-design.md §4.1）：
- 作用域：apply 只动当前项目的 memo，别的项目的 memo id 按条报错不执行；
- 整理权：同一项目同一时刻只有一个会话能整理。candidates 占位（发租约，
  peek=true 只看不占），apply 只认自己那张租约；租约带 TTL、按需判过期，不靠
  任何定时器。
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from aiteam.api.deps import get_scoped_repository
from aiteam.api.routes.memory import (
    _MAX_CONTENT_CHARS,
    _bucket_quota_check,
    _resolve_scope_id,
)
from aiteam.api.schemas import ReconcileApply
from aiteam.api.task_edge import session_id_from_request
from aiteam.memory.content_safety import scan_direction_content, scan_invisible
from aiteam.memory.reconcile import OPERATION_GUIDE, build_candidate_groups
from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskMemo

router = APIRouter(prefix="/api/memory/reconcile", tags=["memory"])

# 候选组超过此数即提示开 ultracode 并发精判（设计 §4 触发条款）。
_ULTRACODE_GROUP_HINT = 8

# 整理权租约时长：覆盖一次 candidates → 精判 → apply；每次 candidates/apply 都会顺延。
# 持有者崩溃后最多挡别的会话这么久（到点即可接管，按需判定，无定时器）。
_LEASE_TTL_SECONDS = 30 * 60

_PROJECT_REQUIRED = "记忆整理需项目上下文——请在项目内调用（X-Project-Id / X-Project-Dir）"

# HTTP MCP 宿主（Codex）没有 CC 会话 id；MCP 侧把该连接的 MCP 会话 id 放在这个头里
# （见 mcp/_base._api_call）。它由服务端签发、一连接一个，丢了 lease_id 也认得出本人。
_MCP_SESSION_HEADER = "X-Aiteam-Mcp-Session-Id"

# candidates 续约时回传的 lease_id 走这个请求头，不走 query：uvicorn 的 access 行
# 连 query string 一起写进 debug.log（见 api/debug_log.py），凭据放在 URL 里就落进了
# 任何会话都能读的日志文件（复核 fc2f61c6）。apply 的 lease_id 在 JSON body 里，不进日志。
_LEASE_HEADER = "X-Aiteam-Reconcile-Lease"


def _holder_identity(request: Request) -> str:
    """整理权的持有者身份：CC 会话 id，否则 HTTP MCP 连接的会话 id，都没有则空。

    两个头互斥：stdio MCP（CC）只发 X-CC-Session-Id，HTTP MCP 只发
    X-Aiteam-Mcp-Session-Id，先看哪个只是防御性排序。两者都由传输层设置，
    工具参数改不了；lease_id 是工具参数，所以库里只能存它的哈希。
    """
    prefix = StorageRepository.RECONCILE_MCP_HOLDER_PREFIX
    cc_session = session_id_from_request(request)
    # A CC session id never starts with "mcp:"; one that does is ignored, so the two
    # identity namespaces cannot collide in holder_hash.
    if cc_session and not cc_session.startswith(prefix):
        return cc_session
    mcp_session = (request.headers.get(_MCP_SESSION_HEADER) or "").strip()
    return f"{prefix}{mcp_session}" if mcp_session else ""


def _held_by_other_view(lease: dict | None) -> dict | None:
    """持有情况的展示形态：持有者类型与会话 id 前 8 位、起止时间。

    库里的记录只有凭据哈希（见 repository.claim_reconcile_lease），这里也不转出哈希，
    只给展示字段。
    """
    if not lease:
        return None
    return {
        "holder_kind": lease.get("holder_kind") or "lease_id_only",
        "holder_session": lease.get("holder_prefix") or "(未带会话身份)",
        "acquired_at": lease.get("acquired_at"),
        "expires_at": lease.get("expires_at"),
    }


def _held_message(lease: dict) -> str:
    """被挡方收到的话：谁拿着、还要等多久、现在能做什么。"""
    view = _held_by_other_view(lease) or {}
    who = {
        "cc_session": f"CC 会话 {view.get('holder_session')}…",
        "mcp_connection": f"HTTP MCP 连接（如 Codex）{view.get('holder_session')}…",
    }.get(view.get("holder_kind"), "一个未带会话身份的调用方")
    minutes = max(1, math.ceil(StorageRepository.reconcile_lease_seconds_left(lease) / 60))
    return (
        f"本项目的整理权由{who}持有，约 {minutes} 分钟后过期（{view.get('expires_at')}），"
        "这次不交出候选。只想看看有什么可整理：带 peek=true 再调（只读，不占整理权）；"
        "要动手整理：等它过期后再调，或请持有者提交空批 memory_reconcile_apply(operations=[]) "
        "释放。持有者就是你自己却没被认出时，回传上次拿到的 lease_id 即可续用。"
    )


def _own_view(lease: dict, status: str, issued_lease_id: str | None = None) -> dict:
    """持有者看到的租约。明文 lease_id 只在新发时给这一次，库里与别的响应都没有它。"""
    view = {
        "status": status,
        "expires_at": lease["expires_at"],
        "ttl_seconds": _LEASE_TTL_SECONDS,
    }
    if issued_lease_id:
        view["lease_id"] = issued_lease_id
        view["note"] = "lease_id 只显示这一次（库里只存哈希）；非 CC、非 HTTP MCP 的调用方请自行保存。"
    else:
        view["note"] = "沿用已持有的整理权；lease_id 不再回显。"
    return view


def _ownership_error(
    op: str, memos: dict[str, TaskMemo | None], project_id: str
) -> dict | None:
    """查得到但不归当前项目的 memo：属于别的项目的与没有项目归属的分开报。

    查不到的另算 not_found。全部归当前项目时返回 None。
    """
    foreign = [
        mid for mid, m in memos.items()
        if m is not None and m.project_id and m.project_id != project_id
    ]
    unowned = [mid for mid, m in memos.items() if m is not None and not m.project_id]
    if not foreign and not unowned:
        return None
    parts = []
    if foreign:
        parts.append(f"{len(foreign)} 条属于别的项目（到那个项目的会话里去整理）")
    if unowned:
        parts.append(
            f"{len(unowned)} 条没有项目归属（project_id 为空，任何项目都整理不了，须先补归属）"
        )
    error: dict = {
        "op": op,
        "status": "error",
        "error": f"本条操作整条未执行：{'；'.join(parts)}。整理只能动当前项目的 memo。",
    }
    if foreign:
        error["foreign_memo_ids"] = foreign
    if unowned:
        error["unowned_memo_ids"] = unowned
    return error


@router.get("/candidates")
async def reconcile_candidates(
    request: Request,
    scope_path: str = Query(
        "", description="仅整理该路径作用域的 memo（留空=全项目有效 memo）"
    ),
    threshold: float = Query(
        0.45, ge=0.0, le=1.0, description="簇内 BM25 相似度配对阈值"
    ),
    lease_id: str = Header(
        "",
        alias=_LEASE_HEADER,
        description=(
            "续用自己持有的整理权时回传（CC 会话与 HTTP MCP 连接自动识别，可不传）；"
            "走请求头，不走 query——URL 会写进访问日志"
        ),
    ),
    peek: bool = Query(
        False, description="只读查看：不取整理权、不发 lease_id，拿到的候选不能拿去 apply"
    ),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """粗筛：返回情景层候选组 + 方向层清单 + 蒸馏素材 + 操作说明。

    同时取得（或续约）本项目的整理权；别的会话持有未过期的整理权时拒绝，
    不交出候选——免得两个会话对着同一批 memo 各判一遍、各写一份摘要。
    peek=true 时只看不占：照常返回候选，不碰整理权（Leader 循环、随手看一眼走这条）。
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail=_PROJECT_REQUIRED)

    if "lease_id" in request.query_params:
        # Too late for this one (the access line already has it), but say so instead of
        # silently treating the caller as a stranger and telling them to pass it back.
        raise HTTPException(
            status_code=400,
            detail=(
                f"lease_id 不接受放在 URL 里（会连同 query 写进访问日志），请改用请求头 "
                f"{_LEASE_HEADER}。已经放进 URL 的这个 lease_id 应视为已泄漏。"
            ),
        )

    holder = _holder_identity(request)
    if peek:
        current = await repo.get_reconcile_lease(project_id)
        live = current if current and not repo.reconcile_lease_expired(current) else None
        lease_view: dict = {
            "status": "peek",
            "held_by": _held_by_other_view(live),
            "held_by_you": repo.reconcile_lease_owned(live, holder, lease_id),
            "note": "只读查看，未占整理权；要应用须不带 peek 重新调用取得整理权。",
        }
    else:
        outcome, lease, issued = await repo.claim_reconcile_lease(
            project_id,
            session_id=holder,
            lease_id=lease_id,
            ttl_seconds=_LEASE_TTL_SECONDS,
            allow_new=True,
        )
        if outcome == "no_project":
            raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
        if outcome == "held":
            return {
                "success": False,
                "error": _held_message(lease),
                "reconcile_lease": _held_by_other_view(lease),
            }
        lease_view = _own_view(lease, outcome, issued)

    memos = await repo.list_project_task_memos(
        project_id, scope_path=scope_path or None
    )
    groups = build_candidate_groups(memos, threshold=threshold)
    promotion = [g for g in groups if g["promotion_candidate"]]

    # 方向层清单：全部有效条目全文，供调用方逐条判"是否仍成立"（陈旧检测）。
    directions = await repo.list_direction_memories(project_id=project_id)
    direction_inventory = [
        {
            "id": m.id,
            "kind": m.kind,
            "scope": m.scope.value,
            "scope_id": m.scope_id,
            "content": m.content,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in directions
    ]

    stats = {
        "project_id": project_id,
        "total_valid_memos": len(memos),
        "candidate_group_count": len(groups),
        "promotion_candidate_count": len(promotion),
        "direction_count": len(direction_inventory),
    }
    if len(groups) > _ULTRACODE_GROUP_HINT:
        stats["ultracode_hint"] = (
            f"候选组 {len(groups)} 个，量大——建议开 ultracode 用 Workflow "
            "并发精判各组后统一 apply。"
        )

    return {
        "success": True,
        "data": {
            "candidate_groups": groups,
            "promotion_candidates": promotion,
            "direction_inventory": direction_inventory,
            "operation_guide": OPERATION_GUIDE,
            "stats": stats,
            "reconcile_lease": lease_view,
        },
    }


@router.post("/apply")
async def reconcile_apply(
    body: ReconcileApply,
    request: Request,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """批量应用整理操作（幂等）。返回逐条结果，末尾刷新 last_reconcile_at。

    先验整理权：只认调用方自己从 candidates 拿到的那张租约（租约被别的会话接管过
    就说明手里的候选可能已过期，整批不执行）。本批全部成功即释放整理权；有报错
    则保留供修正重试，keep_lease=true 时也保留（分批应用）。
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail=_PROJECT_REQUIRED)

    session_id = _holder_identity(request)
    outcome, lease, _ = await repo.claim_reconcile_lease(
        project_id,
        session_id=session_id,
        lease_id=body.lease_id,
        ttl_seconds=_LEASE_TTL_SECONDS,
        allow_new=False,
    )
    if outcome == "no_project":
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    if outcome == "not_held":
        if lease is None:
            reason = (
                "当前没有进行中的整理（整理权未取得——peek 不取整理权——"
                "或已在上一批全部成功后释放）。"
            )
        elif repo.reconcile_lease_expired(lease):
            reason = "整理权记录属于别的会话且已过期：你没有持有整理权，或你那张已被接管过。"
        else:
            reason = "整理权由别的会话持有，对方正在整理本项目。"
        return {
            "success": False,
            "error": (
                f"本批未执行任何操作：{reason}"
                "先调 memory_reconcile_candidates 取得整理权并拿到最新候选，再 apply"
                "（CC 会话与 HTTP MCP 连接自动识别，其他调用方回传 reconcile_lease.lease_id）。"
            ),
            "reconcile_lease": _held_by_other_view(lease),
        }
    results: list[dict] = []

    for op in body.operations:
        kind = (op.op or "").lower().strip()

        if kind in ("keep", "noop", ""):
            results.append({"op": kind or "noop", "status": "noop"})
            continue

        if kind == "invalidate":
            fetched = {mid: await repo.get_task_memo(mid) for mid in op.memo_ids}
            refused = _ownership_error("invalidate", fetched, project_id)
            if refused:
                results.append(refused)
                continue
            invalidated: list[str] = []
            noop: list[str] = []
            missing: list[str] = []
            for mid, existing in fetched.items():
                if existing is None:
                    missing.append(mid)
                    continue
                if existing.invalid_at is not None:
                    noop.append(mid)  # 已失效重复操作 → noop
                    continue
                await repo.invalidate_task_memo(mid)
                invalidated.append(mid)
            results.append(
                {
                    "op": "invalidate",
                    "status": "applied" if invalidated else "noop",
                    "invalidated": invalidated,
                    "already_invalid": noop,
                    "not_found": missing,
                }
            )
            continue

        if kind == "merge":
            content = (op.content or "").strip()
            if not content:
                results.append(
                    {"op": "merge", "status": "error", "error": "merge 需要 content"}
                )
                continue
            # merge 建的是一条新 memo，与 task_memo_add 同规：不可见字符拒收（逐条报错，
            # 同批其他操作照常执行）。此前这条路不扫，是 memo 写入口里唯一的缺口。
            finding = scan_invisible(content)
            if finding is not None:
                results.append(
                    {
                        "op": "merge",
                        "status": "error",
                        "error": finding.message,
                        "safety": {"category": finding.category, "pattern": finding.pattern},
                    }
                )
                continue
            fetched = {mid: await repo.get_task_memo(mid) for mid in op.memo_ids}
            refused = _ownership_error("merge", fetched, project_id)
            if refused:
                results.append(refused)
                continue
            # 只并入仍有效的 memo；全部已失效 → noop（幂等）
            valid = [m for m in fetched.values() if m is not None and m.invalid_at is None]
            if not valid:
                results.append(
                    {
                        "op": "merge",
                        "status": "noop",
                        "reason": "无有效待并 memo（可能已被整理）",
                    }
                )
                continue
            base = valid[0]
            new_memo = await repo.add_task_memo(
                base.task_id,
                content=content,
                author="reconcile",
                memo_type=op.memo_type or "summary",
                scope_path=op.scope_path or base.scope_path,
                project_id=base.project_id,
            )
            for m in valid:
                await repo.invalidate_task_memo(m.id, invalidated_by=new_memo.id)
            results.append(
                {
                    "op": "merge",
                    "status": "applied",
                    "new_memo_id": new_memo.id,
                    "merged": [m.id for m in valid],
                }
            )
            continue

        if kind == "score":
            if not op.memo_id or op.quality_score is None:
                results.append(
                    {
                        "op": "score",
                        "status": "error",
                        "error": "score 需要 memo_id 与 quality_score",
                    }
                )
                continue
            if not (1 <= op.quality_score <= 10):
                results.append(
                    {
                        "op": "score",
                        "status": "error",
                        "error": "quality_score 取值 1-10",
                    }
                )
                continue
            target = await repo.get_task_memo(op.memo_id)
            if target is None:
                results.append(
                    {"op": "score", "status": "error", "error": f"memo {op.memo_id} 不存在"}
                )
                continue
            refused = _ownership_error("score", {op.memo_id: target}, project_id)
            if refused:
                results.append(refused)
                continue
            scored = await repo.score_task_memo(
                op.memo_id, op.quality_score, op.reason
            )
            if scored is None:
                results.append(
                    {"op": "score", "status": "error", "error": f"memo {op.memo_id} 不存在"}
                )
                continue
            results.append(
                {
                    "op": "score",
                    "status": "applied",
                    "memo_id": op.memo_id,
                    "quality_score": op.quality_score,
                }
            )
            continue

        if kind == "promote":
            content = (op.content or "").strip()
            if not content:
                results.append(
                    {"op": "promote", "status": "error", "error": "promote 需要 content"}
                )
                continue
            if op.scope not in ("global", "project", "user"):
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": f"promote scope 只能是 global/project/user，收到 {op.scope!r}",
                    }
                )
                continue
            if op.kind not in repo.DIRECTION_KINDS:
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": f"kind 只能是 {'/'.join(repo.DIRECTION_KINDS)}，收到 {op.kind!r}",
                    }
                )
                continue
            # 安全扫描：promote 是方向层的第二道写入口，与 memory_add 同规
            finding = scan_direction_content(content)
            if finding is not None:
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": finding.message,
                        "safety": {
                            "category": finding.category,
                            "pattern": finding.pattern,
                        },
                    }
                )
                continue
            # 红线①：单条 ≤ 400 字
            if len(content) > _MAX_CONTENT_CHARS:
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": (
                            f"内容 {len(content)} 字超方向层单条上限 {_MAX_CONTENT_CHARS}——"
                            "请精简或改指针条目"
                        ),
                    }
                )
                continue
            scope_id = _resolve_scope_id(op.scope, "", repo)
            # 红线②：桶字符配额（存储上限 = 注入预算，与 memory_add 同一根轴）。
            # 这里不回挂全桶清单——整理流程的 direction_inventory 已给过全文，
            # 每条 promote 失败都复述一遍只会把响应撑爆。
            over_quota = await _bucket_quota_check(
                repo, op.scope, scope_id, len(content), include_entries=False
            )
            if over_quota is not None:
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": over_quota["error"],
                        "quota": over_quota["quota"],
                    }
                )
                continue
            memory = await repo.create_memory(
                scope=op.scope,
                scope_id=scope_id,
                content=content,
                kind=op.kind,
                source_refs=op.source_refs,
            )
            results.append(
                {"op": "promote", "status": "applied", "memory_id": memory.id}
            )
            continue

        results.append(
            {"op": kind, "status": "error", "error": f"未知操作 {kind!r}"}
        )

    # 刷新整理时间戳（复用 project.config，不建新表）——量阈软提示的基线。
    reconciled_at = None
    if any(r.get("status") == "applied" for r in results):
        when = await repo.set_last_reconcile_at(project_id)
        reconciled_at = when.isoformat() if when else None

    errors = sum(1 for r in results if r.get("status") == "error")
    if errors or body.keep_lease:
        lease_view = _own_view(lease, "retained")
        lease_view["reason"] = (
            f"{errors} 条操作报错，整理权保留供修正后重试；完成后整批成功即释放。"
            if errors
            else "keep_lease=true：整理权保留，最后一批不传 keep_lease 即释放。"
        )
    else:
        await repo.release_reconcile_lease(
            project_id, session_id=session_id, lease_id=body.lease_id
        )
        lease_view = {"status": "released"}

    return {
        "success": True,
        "data": {
            "results": results,
            "applied_count": sum(1 for r in results if r.get("status") == "applied"),
            "last_reconcile_at": reconciled_at,
            "reconcile_lease": lease_view,
        },
    }
