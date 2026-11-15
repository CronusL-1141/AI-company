"""AI Team OS: multi-host sync between nodes (aiteam.sync).

The center serves its edges here: the handshake, the operations they send, the
changes their replicas follow, rows to settle staged writes against, the conflict
list, and the wakes of their sessions. Every node answers the handshake and its
own status. Edges reach their center over a loopback port forwarded by SSH; no
route here is meant for anything else.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from aiteam.api.deps import get_center, get_repository, get_sync_node
from aiteam.storage.repository import StorageRepository
from aiteam.sync import status as sync_status
from aiteam.sync.center import Center
from aiteam.sync.feed import hello
from aiteam.sync.operations import OperationRefusedError, TemporaryFailureError
from aiteam.sync.protocol import CHANGES_PAGE
from aiteam.types import (
    NodeStatus,
    RouteSurface,
    SyncChange,
    SyncChangesPage,
    SyncConflict,
    SyncHello,
    SyncOperation,
    SyncOpResult,
    SyncStatus,
)

router = APIRouter(prefix="/api/sync", tags=["sync"])
# Sync face of these routes (aiteam.sync.surface): the sync protocol itself, node to node.
ROUTE_SURFACE = RouteSurface.local()


class NodeBody(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)


class PreferBody(NodeBody):
    batch: str | None = Field(default=None, max_length=36)  # default: the node's latest batch


class RowsBody(BaseModel):
    keys: list[tuple[str, str]] = Field(default_factory=list, max_length=1000)


def _center() -> Center:
    center = get_center()
    if center is None:
        raise HTTPException(status_code=409, detail="本节点不是多机同步的中心")
    return center


@router.get("/hello", response_model=SyncHello)
async def sync_hello(
    cursor: int | None = Query(None), cursor_hlc: str = Query("", max_length=128),
    node: NodeStatus = Depends(get_sync_node), repo: StorageRepository = Depends(get_repository),
) -> SyncHello:
    """Who this node is and what it syncs under (the handshake); given an edge's cursor and
    the clock it recorded there, whether that change is still where it was."""
    return await hello(repo.db_url, node.node_id, node.role, cursor=cursor, cursor_hlc=cursor_hlc)


@router.get("/status", response_model=SyncStatus)
async def sync_node_status(
    node: NodeStatus = Depends(get_sync_node), repo: StorageRepository = Depends(get_repository),
) -> SyncStatus:
    """This node's part in the sync; ``healthy`` false with ``problems`` when it needs a look."""
    return await sync_status.read(node, repo.db_url, get_center())


@router.post("/ops", response_model=SyncOpResult)
async def sync_apply_operation(op: SyncOperation, center: Center = Depends(_center)) -> SyncOpResult:
    """Carry out one operation of an edge, exactly once (a repeat gets the recorded answer)."""
    try:
        return await center.operations.apply(op)
    except OperationRefusedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TemporaryFailureError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/changes", response_model=SyncChangesPage)
async def sync_changes(
    after: int = Query(0, ge=0), limit: int = Query(CHANGES_PAGE, ge=1, le=5000),
    center: Center = Depends(_center),
) -> SyncChangesPage:
    """Shared-face rows changed after ``after``, for an edge's replica."""
    return await center.feed.changes(after, limit)


@router.get("/snapshot")
async def sync_snapshot(
    table: str, after: int = Query(0, ge=0), limit: int = Query(CHANGES_PAGE, ge=1, le=5000),
    center: Center = Depends(_center),
) -> dict[str, Any]:
    """One page of a shared table, for an edge rebuilding its replica."""
    try:
        return await center.feed.snapshot(table, after, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rows")
async def sync_rows(body: RowsBody, center: Center = Depends(_center)) -> dict[str, list[SyncChange]]:
    """Current rows by key (None for a row that is gone), for settling staged writes."""
    return {"rows": await center.feed.rows(body.keys)}


@router.post("/drained")
async def sync_drained(body: NodeBody, center: Center = Depends(_center)) -> dict[str, Any]:
    """An edge emptied its queue: brief the user once on the conflicts it met."""
    return {"briefing_id": await center.conflicts.drained(body.node_id)}


@router.post("/wakes/claim")
async def sync_claim_wakes(body: NodeBody, center: Center = Depends(_center)) -> dict[str, Any]:
    """Due scheduled wakes of the node's sessions, handed over for it to run."""
    claimed = await center.wakes.claim(body.node_id)
    return {"claimed": [task.model_dump(mode="json") for task in claimed]}


@router.get("/conflicts")
async def sync_conflicts(
    status: str | None = None, loser: str | None = None, batch: str | None = None,
    limit: int = Query(100, ge=1, le=1000), center: Center = Depends(_center),
) -> dict[str, list[SyncConflict]]:
    """Decisions between writes, newest first; ``loser`` is the node whose write lost,
    ``batch`` the briefing that reported them."""
    return {"items": await center.conflicts.list(status=status, loser=loser, batch=batch, limit=limit)}


@router.post("/conflicts/prefer")
async def sync_prefer_node(body: PreferBody, center: Center = Depends(_center)) -> dict[str, list[SyncConflict]]:
    """Apply every value ``node_id`` lost in one batch (default: its latest), as new writes;
    a field written again since its decision is left alone (superseded)."""
    return {"items": await center.conflicts.prefer(body.node_id, body.batch)}


@router.post("/conflicts/{conflict_id}/flip", response_model=SyncConflict)
async def sync_flip_conflict(conflict_id: str, center: Center = Depends(_center)) -> SyncConflict:
    """Apply the losing value of one decision after all."""
    try:
        return await center.conflicts.flip(conflict_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"冲突 {conflict_id} 不存在") from exc
