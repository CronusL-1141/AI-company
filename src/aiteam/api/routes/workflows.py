"""AI Team OS — Workflow observability routes (I3a).

CC ultracode/Workflow 运行档案 + 逐-agent 遥测的只读查询 + 手动对账端点。

项目隔离用可选 ``?project_id=`` query（非 ``X-Project-Id`` 头）：前端 apiFetch 只对
``/api/ecosystem`` 附加隔离头（client.ts 硬 gate），非 ecosystem 端点用 get_repository
全局仓 + 显式 query 过滤，避免重演 teams 全消失 bug（守红线2 不动 project_scope）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from aiteam.api.deps import get_event_bus, get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowAgent, WorkflowRun

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class WorkflowReconcileBody(BaseModel):
    """POST /reconcile 请求体（全可选）。"""

    session_id: str | None = None
    project_dir: str | None = None


class WorkflowReconcileResult(BaseModel):
    """对账结果计数。"""

    success: bool = True
    ingested: int = 0
    updated: int = 0
    errors: int = 0
    scanned: int = 0


@router.get("", response_model=APIListResponse[WorkflowRun])
async def list_workflow_runs(
    status: str = Query("", description="按状态过滤：planned/running/completed/interrupted/killed/failed"),
    project_id: str = Query("", description="按项目过滤（空=全部项目）"),
    limit: int = Query(50, ge=1, le=200),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[WorkflowRun]:
    """列出已物化的 workflow 运行（天然排除 workflow-session-* 兜底团队）。"""
    runs = await repo.list_workflow_runs(project_id=project_id, status=status, limit=limit)
    return APIListResponse(data=runs, total=len(runs))


@router.get("/{wf_id}", response_model=WorkflowRun)
async def get_workflow_run(
    wf_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> WorkflowRun:
    """运行详情（phases、计划-vs-实际、run 级总量、summary/result）。"""
    run = await repo.get_workflow_run(wf_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Workflow run '{wf_id}' not found")
    return run


@router.get("/{wf_id}/agents", response_model=APIListResponse[WorkflowAgent])
async def list_workflow_agents(
    wf_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[WorkflowAgent]:
    """逐-agent 遥测（label/model/tokens/toolCalls/duration/state/lastTool/resultPreview）。"""
    agents = await repo.list_workflow_agents(wf_id)
    return APIListResponse(data=agents, total=len(agents))


@router.post("/reconcile", response_model=WorkflowReconcileResult)
async def reconcile_workflows(
    body: WorkflowReconcileBody | None = None,
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> WorkflowReconcileResult:
    """手动对账：扫 proj-slug 下 workflows/wf_*.json 逐文件 ingest。

    SessionStart hook + Dashboard「刷新」+ MCP workflow_reconcile 共用同一纯函数。
    """
    from aiteam.api import workflow_ingest

    body = body or WorkflowReconcileBody()
    result = await workflow_ingest.reconcile(
        repo,
        event_bus,
        project_dir=body.project_dir,
        session_id=body.session_id,
    )

    # Phase2 可选增强：对 running/interrupted run 顺带 live tail + .output 富化，
    # 手动刷新即时看 live、不等 60s reaper tick。best-effort，失败不影响对账结果。
    live: list[WorkflowRun] = []
    for st in ("running", "interrupted"):
        try:
            live.extend(await repo.list_workflow_runs(status=st, limit=200))
        except Exception:  # noqa: BLE001
            pass
    live.sort(key=lambda r: r.updated_at or r.created_at)
    for run in live[: workflow_ingest.WF_LIVE_TAIL_MAX_RUNS]:
        try:
            res = await workflow_ingest.tail_live_run(repo, event_bus, run)
            newly_marked = bool(isinstance(res, dict) and res.get("marked_interrupted"))
            if run.status == "interrupted" or newly_marked:
                await workflow_ingest.enrich_from_task_output(repo, run)
        except Exception:  # noqa: BLE001 — 手动对账不因 live 富化失败而报错
            pass

    return WorkflowReconcileResult(
        success=True,
        ingested=result.get("ingested", 0),
        updated=result.get("updated", 0),
        errors=result.get("errors", 0),
        scanned=result.get("scanned", 0),
    )
