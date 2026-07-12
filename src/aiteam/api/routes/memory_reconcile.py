"""AI Team OS — Memory reconcile 路由（记忆系统 v2 P2）.

设计 §4「按需整理」两端点：
- GET  /api/memory/reconcile/candidates —— 确定性粗筛（零 LLM）：情景层候选组
  （簇内 BM25 配对）+ 方向层清单（供逐条陈旧检查）+ 蒸馏素材（promotion 候选）+
  操作说明。判定交给调用工具的 CC 会话内 agent（OS 无独立 LLM 凭据）。
- POST /api/memory/reconcile/apply —— 批量应用 agent 确认后的操作
  （merge/invalidate/score/promote），幂等：对已失效条目重复操作返回 noop。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api.deps import get_scoped_repository
from aiteam.api.routes.memory import (
    _MAX_CONTENT_CHARS,
    _MAX_VALID_PER_BUCKET,
    _resolve_scope_id,
)
from aiteam.api.schemas import ReconcileApply
from aiteam.memory.reconcile import OPERATION_GUIDE, build_candidate_groups
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/memory/reconcile", tags=["memory"])

# 候选组超过此数即提示开 ultracode 并发精判（设计 §4 触发条款）。
_ULTRACODE_GROUP_HINT = 8


@router.get("/candidates")
async def reconcile_candidates(
    scope_path: str = Query(
        "", description="仅整理该路径作用域的 memo（留空=全项目有效 memo）"
    ),
    threshold: float = Query(
        0.45, ge=0.0, le=1.0, description="簇内 BM25 相似度配对阈值"
    ),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """粗筛：返回情景层候选组 + 方向层清单 + 蒸馏素材 + 操作说明。"""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail="记忆整理需项目上下文——请在项目内调用（X-Project-Id / X-Project-Dir）",
        )

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
        },
    }


@router.post("/apply")
async def reconcile_apply(
    body: ReconcileApply,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """批量应用整理操作（幂等）。返回逐条结果，末尾刷新 last_reconcile_at。"""
    project_id = repo._project_scope
    results: list[dict] = []

    for op in body.operations:
        kind = (op.op or "").lower().strip()

        if kind in ("keep", "noop", ""):
            results.append({"op": kind or "noop", "status": "noop"})
            continue

        if kind == "invalidate":
            invalidated: list[str] = []
            noop: list[str] = []
            missing: list[str] = []
            for mid in op.memo_ids:
                existing = await repo.get_task_memo(mid)
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
            # 只并入仍有效的 memo；全部已失效 → noop（幂等）
            valid = []
            for mid in op.memo_ids:
                m = await repo.get_task_memo(mid)
                if m is not None and m.invalid_at is None:
                    valid.append(m)
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
            # 红线②：同桶有效条目 ≤ 40
            valid_count = await repo.count_valid_memories(op.scope, scope_id)
            if valid_count >= _MAX_VALID_PER_BUCKET:
                results.append(
                    {
                        "op": "promote",
                        "status": "error",
                        "error": (
                            f"作用域 {op.scope}/{scope_id} 已有 {valid_count} 条有效方向记忆，"
                            f"达上限 {_MAX_VALID_PER_BUCKET}——先合并/失效冗余再提升"
                        ),
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
    if project_id:
        applied_any = any(r.get("status") == "applied" for r in results)
        if applied_any:
            when = await repo.set_last_reconcile_at(project_id)
            reconciled_at = when.isoformat() if when else None

    return {
        "success": True,
        "data": {
            "results": results,
            "applied_count": sum(1 for r in results if r.get("status") == "applied"),
            "last_reconcile_at": reconciled_at,
        },
    }
