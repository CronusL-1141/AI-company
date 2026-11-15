"""The center's list of decisions between writes, and how the user overrides them.

* ``drained``: an edge emptied its queue; the conflicts its operations met become
  one batch, told to the user in one briefing (both in one transaction).
* ``flip``: apply one losing value after all, as a new write on the center.
* ``prefer``: apply every value one node lost in one batch (by default its latest).
* A decision whose field was written again after it was taken is not flipped back
  to the old value; it is marked superseded instead.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from aiteam.clock import utc_now
from aiteam.storage.connection import get_engine, unit_of_work
from aiteam.storage.models import SyncConflictModel
from aiteam.storage.repository import StorageRepository
from aiteam.sync.merge import UPDATE_ROUTES
from aiteam.sync.routing import call_app, refused
from aiteam.types import SyncConflict, SyncConflictStatus

_OPEN = (SyncConflictStatus.AUTO.value, SyncConflictStatus.PENDING.value)


class ConflictDesk:
    """Conflicts on file at the center: listing, batching, flipping."""

    def __init__(self, app: Any, repo: StorageRepository) -> None:
        self._app = app
        self._repo = repo
        self._db_url = repo.db_url

    async def list(self, *, status: str | None = None, loser: str | None = None, batch: str | None = None,
                   ids: list[str] | None = None, limit: int = 100) -> list[SyncConflict]:
        def read(conn: Connection) -> list[SyncConflict]:
            from sqlalchemy.orm import Session

            query = select(SyncConflictModel).order_by(SyncConflictModel.created_at.desc()).limit(limit)
            for column, value in ((SyncConflictModel.status, status), (SyncConflictModel.loser_node, loser),
                                  (SyncConflictModel.batch, batch)):
                if value is not None:
                    query = query.where(column == value)
            if ids is not None:
                query = query.where(SyncConflictModel.id.in_(ids))
            with Session(bind=conn) as orm:
                return [_conflict(row) for row in orm.scalars(query)]

        async with get_engine(self._db_url).connect() as conn:
            return await conn.run_sync(read)

    async def drained(self, node_id: str) -> str | None:
        """An edge emptied its queue: brief the user once on the conflicts its operations met.

        The briefing and the batch it names commit together, under the write lock, so
        two drains reported at once never brief the same conflicts twice.
        """
        async def lock(session: AsyncSession) -> None:
            await session.execute(text("UPDATE sync_state SET hlc = hlc WHERE id = 1"))

        async with unit_of_work(lock, self._db_url) as session:
            ids = [row[0] for row in (await session.execute(text(
                "SELECT c.id FROM sync_conflicts c JOIN sync_op_receipts r ON r.op_id = c.op_id "
                "WHERE c.batch = '' AND r.node = :node"), {"node": node_id})).all()]
            if not ids:
                return None
            briefing = await self._repo.create_briefing(**_briefing(node_id, await self.list(ids=ids, limit=len(ids))))
            await session.execute(
                text("UPDATE sync_conflicts SET batch = :batch WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)), {"batch": briefing.id, "ids": ids})
        return briefing.id

    async def flip(self, conflict_id: str) -> SyncConflict:
        """Apply the losing value of one decision after all, as a new write here."""
        found = await self.list(ids=[conflict_id], limit=1)
        if not found:
            raise LookupError(conflict_id)
        return await self._flip(found[0])

    async def prefer(self, node_id: str, batch: str | None = None) -> list[SyncConflict]:
        """Apply every value ``node_id`` lost in one batch, oldest first (default: its latest)."""
        if batch is None:
            async with get_engine(self._db_url).connect() as conn:
                batch = (await conn.exec_driver_sql(
                    "SELECT batch FROM sync_conflicts WHERE loser_node = ? AND batch != '' "
                    "ORDER BY created_at DESC LIMIT 1", (node_id,))).scalar() or ""
        lost = await self.list(loser=node_id, batch=batch, limit=10_000)
        return [await self._flip(conflict) for conflict in reversed(lost) if conflict.status.value in _OPEN]

    async def _flip(self, conflict: SyncConflict) -> SyncConflict:
        request = _flip_request(conflict)
        if request is None:
            return conflict  # nothing to write (both replacements kept, a field with no route)
        if conflict.field and await self._written_since(conflict):
            return await self._mark(conflict, SyncConflictStatus.SUPERSEDED,
                                    "裁决之后该字段又被写过，不再翻回旧值")
        method, path, query, payload = request
        status, _, body = await call_app(self._app, method, path, query=query,
                                         headers={"content-type": "application/json"}, body=payload)
        if refused(status, body):
            return await self._mark(conflict, SyncConflictStatus.PENDING,
                                    f"翻转失败（{status}）：{body.decode('utf-8', 'replace')[:500]}")
        return await self._mark(conflict, SyncConflictStatus.FLIPPED, conflict.detail)

    async def _written_since(self, conflict: SyncConflict) -> bool:
        async with get_engine(self._db_url).connect() as conn:
            now = (await conn.exec_driver_sql(
                "SELECT hlc FROM sync_field_clock WHERE table_name = ? AND pk = ? AND field = ?",
                (conflict.table_name, conflict.pk, conflict.field))).scalar()
        return (now or "") != conflict.winner_hlc

    async def _mark(self, conflict: SyncConflict, status: SyncConflictStatus, detail: str) -> SyncConflict:
        now = utc_now() if status is not SyncConflictStatus.PENDING else None
        async with get_engine(self._db_url).begin() as conn:
            await conn.execute(
                text("UPDATE sync_conflicts SET status = :s, detail = :d, resolved_at = :r WHERE id = :id"),
                {"s": status.value, "d": detail, "r": now.isoformat() if now else None, "id": conflict.id})
        return conflict.model_copy(update={"status": status, "detail": detail, "resolved_at": now})


def _briefing(node_id: str, conflicts: list[SyncConflict]) -> dict[str, Any]:
    pending = sum(c.status is SyncConflictStatus.PENDING for c in conflicts)
    lines = [f"- [{c.status.value}] {c.table_name or c.path} {c.pk} {c.field}：{c.rule}" for c in conflicts[:20]]
    return {
        "title": f"{node_id} 重连后有 {len(conflicts)} 处同步冲突（{pending} 处待处理）",
        "description": "多机同步按规则自动裁决，输掉的值都已留档；待处理的是规则无法裁决的写入。\n" + "\n".join(lines),
        "options": f"逐条翻转（POST /api/sync/conflicts/{{id}}/flip），或这一批以 {node_id} 为准"
                   f"（POST /api/sync/conflicts/prefer，node_id={node_id}，batch 为本条简报的 id）。",
        "urgency": "high" if pending else "medium", "tags": ["sync-conflict"],
    }


def _flip_request(conflict: SyncConflict) -> tuple[str, str, str, bytes] | None:
    """The write that applies a lost value after all: the field's update route, or the
    refused operation itself."""
    if conflict.field:
        if conflict.table_name not in UPDATE_ROUTES:
            return None
        method, template = UPDATE_ROUTES[conflict.table_name]
        payload = json.dumps({conflict.field: conflict.loser}, ensure_ascii=False).encode()
        return method, template.format(key=conflict.pk), "", payload
    if not conflict.method:
        return None
    path, _, query = conflict.path.partition("?")
    payload = json.dumps(conflict.loser, ensure_ascii=False).encode() if conflict.loser is not None else b""
    return conflict.method, path, query, payload


def _conflict(row: SyncConflictModel) -> SyncConflict:
    return SyncConflict(
        id=row.id, op_id=row.op_id, loser_node=row.loser_node, table_name=row.table_name, pk=row.pk,
        field=row.field, winner=row.winner, loser=row.loser, rule=row.rule, winner_hlc=row.winner_hlc,
        status=SyncConflictStatus(row.status), detail=row.detail, method=row.method, path=row.path,
        batch=row.batch, created_at=row.created_at, resolved_at=row.resolved_at,
    )
