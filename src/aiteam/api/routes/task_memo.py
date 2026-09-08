"""AI Team OS — Task memo tracking routes.

Provides task memo read and append functionality for recording task progress, decisions, issues, and summaries.
记忆系统 v2 P0：memo 已从 Task.config["memo"] JSON 数组升为独立 task_memos 表；
写入接口保持完全兼容，读写均走表（默认过滤失效条目 invalid_at IS NULL）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.api.schemas import MemoEntry
from aiteam.api.task_edge import try_record_worked_on
from aiteam.memory.content_safety import scan_invisible
from aiteam.storage.repository import StorageRepository, _task_memo_to_legacy

logger = logging.getLogger(__name__)

router = APIRouter(tags=["task-memo"])

# 这里**刻意不放**「新增 memo 过阈就建议整理」的软提示（记忆 v2 P2 曾有，2026-09-08
# 移除）。三条理由，改回去前先读完：
# ① 指标错配：memo 增量与方向层压力无关。情景层不进任何注入面，堆多少都不花上下文；
#    真正有上限的是方向层（memory.py 桶配额 global 1200 / project 1500 / user 300 字）。
# ② 上位机制已存在且更硬：方向层超限是**写入那一刻拒绝**，并按 Hermes 协议把整桶条目
#    连全文交回要求先整理（memory.py:223）。预警一个已经硬拦的东西是多余的一层。
# ③ 它推向的操作有损：reconcile 的 merge/invalidate 会把原 memo 置 invalid，而 memo
#    的默认读路径过滤 invalid（repository.py:1809-1813），摘要不可逆推回原文——正撞
#    「删数据前问删了能不能重建」。memo 是按需检索的过程档案，不是待压缩的缓存。
# reconcile 工具本身保留：需要从 memo 蒸馏方向层时由 Leader 主动调，不做路径推送。


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
    request: Request,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Append a memo record（写入 task_memos 表；supersedes 给定则置换旧条）。"""
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    # 写入安全扫描（v2.1）：情景层**只扫不可见 Unicode**。memo 是高频路径，且不进
    # 任何 agent 的 system prompt——注入句式在这里只是被记录的文本，不是被执行的
    # 指令；但肉眼不可见的内容无论进哪一层都不该入库（检索会把它捞回模型眼前）。
    finding = scan_invisible(body.content or "")
    if finding is not None:
        return {
            "success": False,
            "error": finding.message,
            "safety": {"category": finding.category, "pattern": finding.pattern},
        }

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

    # 归因 v1 §2.4：在这个**已经必然发生**的记账动作里顺手记下 agent→task 边。
    # 参数里天然同时带着 task_id 与 author，不需要谁多调一次工具。
    await try_record_worked_on(
        repo,
        task_id=task_id,
        author=body.author,
        request=request,
        project_id=task.project_id or "",
        origin="task_memo_add",
    )

    return {"success": True, "data": entry}
