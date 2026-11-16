"""AI Team OS: multi-host sync between nodes (aiteam.sync).

The center serves its edges here: the handshake, the operations they send, their
session faces going up, the changes their replicas follow, rows to settle staged
writes against, the conflict list, and the wakes of their sessions. Every node
answers the handshake, its own status and the nodes whose rows it holds. Edges
reach their center over a loopback port forwarded by SSH; no route here is meant
for anything else.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from aiteam.api.deps import get_center, get_edge, get_repository, get_sync_node
from aiteam.storage.repository import StorageRepository
from aiteam.sync import status as sync_status
from aiteam.sync.center import Center
from aiteam.sync.feed import hello
from aiteam.sync.operations import OperationRefusedError, TemporaryFailureError
from aiteam.sync.protocol import CHANGES_PAGE
from aiteam.types import (
    NodeRole,
    NodeStatus,
    RouteSurface,
    SyncChange,
    SyncChangesPage,
    SyncConflict,
    SyncHello,
    SyncNodes,
    SyncOperation,
    SyncOpResult,
    SyncSessionAck,
    SyncSessionBatch,
    SyncSessionKeys,
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
    edge: str = Query("", alias="node", max_length=64),
    node: NodeStatus = Depends(get_sync_node), repo: StorageRepository = Depends(get_repository),
) -> SyncHello:
    """Who this node is and what it syncs under (the handshake); given an edge's cursor and
    the clock it recorded there, whether that change is still where it was; given the
    edge's id, where its session face stands here."""
    # A node whose identity did not hold answers as no center: its edges stay detached.
    role = node.role if node.channel else NodeRole.STANDALONE
    answer = await hello(repo.db_url, node.node_id, role, cursor=cursor, cursor_hlc=cursor_hlc)
    center = get_center()
    if edge and center is not None:
        answer.intake_cursor = await center.intake.cursor(edge)
    return answer


@router.get("/nodes", response_model=SyncNodes)
async def sync_nodes(node: NodeStatus = Depends(get_sync_node)) -> SyncNodes:
    """The nodes whose rows this node holds: itself, and its edges (center) or its center (edge)."""
    nodes = [node.node_id] if node.node_id else []
    center, edge = get_center(), get_edge()
    if center is not None:
        nodes += [status.node for status in await center.intake.statuses()]
    center_url = ""
    if edge is not None:
        center_url = edge.center_url
        center_id = (await edge.peer()).center_node_id
        nodes += [center_id] if center_id else []
    return SyncNodes(role=node.role, node_id=node.node_id, nodes=nodes, center_url=center_url)


@router.get("/status", response_model=SyncStatus)
async def sync_node_status(
    node: NodeStatus = Depends(get_sync_node), repo: StorageRepository = Depends(get_repository),
) -> SyncStatus:
    """This node's part in the sync; ``healthy`` false with ``problems`` when it needs a look."""
    return await sync_status.read(node, repo.db_url, get_center())


@router.post("/ops", response_model=SyncOpResult)
async def sync_apply_operation(op: SyncOperation, center: Center = Depends(_center)) -> SyncOpResult:
    """Carry out one operation of an edge, exactly once (a repeat gets the recorded answer).

    A forward may carry the session face it waits for (``session_tail``): taken in
    first, as any batch is. 503 until the session face is in as far as the operation
    waits for (``session_behind``); the edge retries, nothing is dropped.
    """
    try:
        if op.session_tail is not None:
            if op.session_tail.origin_node != op.origin_node:
                raise OperationRefusedError("会话面尾巴与操作来自不同节点")
            await center.intake.apply(op.session_tail, pace=False)
        return await center.operations.apply(op)
    except (OperationRefusedError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TemporaryFailureError as exc:
        raise HTTPException(status_code=503, detail=exc.detail()) from exc


@router.post("/session", response_model=SyncSessionAck)
async def sync_take_session(batch: SyncSessionBatch, center: Center = Depends(_center)) -> SyncSessionAck:
    """Take in one batch of an edge's session face (in order; a gap or a stale batch is
    answered with where the edge stands here, and the edge sends again from there)."""
    try:
        return await center.intake.apply(batch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/session/keys", response_model=SyncSessionKeys)
async def sync_session_keys(
    node: str = Query(min_length=1, max_length=64), table: str = Query(min_length=1),
    after: int = Query(0, ge=0), limit: int = Query(CHANGES_PAGE, ge=1, le=5000),
    center: Center = Depends(_center),
) -> SyncSessionKeys:
    """Keys of an edge's session rows held here, page by page (``resend-session``)."""
    try:
        return await center.intake.held_keys(node, table, after, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
