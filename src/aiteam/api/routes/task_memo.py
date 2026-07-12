"""AI Team OS — Task memo tracking routes.

Provides task memo read and append functionality for recording task progress, decisions, issues, and summaries.
记忆系统 v2 P0：memo 已从 Task.config["memo"] JSON 数组升为独立 task_memos 表；
写入接口保持完全兼容，读写均走表（默认过滤失效条目 invalid_at IS NULL）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.api.schemas import MemoEntry
from aiteam.storage.repository import StorageRepository, _task_memo_to_legacy

logger = logging.getLogger(__name__)

router = APIRouter(tags=["task-memo"])


@router.get("/api/tasks/{task_id}/memo")
async def get_task_memo(
    task_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """Get task memo record list（直查 task_memos 表，默认只返回有效条目）。"""
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    memos = await repo.list_task_memos(task_id)
    return {"success": True, "data": [_task_memo_to_legacy(m) for m in memos]}


@router.post("/api/tasks/{task_id}/memo")
async def add_task_memo(
    task_id: str,
    body: MemoEntry,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Append a memo record（写入 task_memos 表；supersedes 给定则置换旧条）。"""
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    memo = await repo.add_task_memo(
        task_id,
        content=body.content,
        author=body.author,
        memo_type=body.type,
        project_id=task.project_id,
        supersedes=body.supersedes,
    )
    entry = _task_memo_to_legacy(memo)

    # 知识层 P1a：抽取跨域引用建边（零 LLM 正则，best-effort 绝不阻塞写入）。
    # 挂路由层 = MCP 工具与 REST 双入口的汇聚点。from_id 用真 memo id。
    try:
        from aiteam.api.link_extract import extract_refs
        from aiteam.types import KnowledgeLink

        refs = extract_refs(body.content)
        if refs:
            await repo.insert_knowledge_links([
                KnowledgeLink(
                    from_kind="task_memo",
                    from_id=memo.id,
                    to_kind=r.to_kind,
                    to_id=r.to_id,
                    link_type=r.link_type,
                    context=r.context,
                    link_source="regex-memo",
                    project_id=task.project_id or "",
                )
                for r in refs
            ])
    except Exception:  # noqa: BLE001
        logger.warning("memo link extraction failed", exc_info=True)

    return {"success": True, "data": entry}
