"""The center carries out its edges' operations, each exactly once.

A write an edge may queue offline runs in a unit of work
(aiteam.storage.connection.unit_of_work) with its receipt, its field clocks and its
conflicts: it commits whole or not at all, and a repeat gets the recorded answer.
Those run one at a time (they are short: one handler, one transaction). Writes only
ever forwarded online (deletions, the ecosystem archive) may call slow services, so
they run outside one and record their receipt afterwards, concurrently with the rest;
only two arrivals of the same operation wait for each other.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aiteam.clock import from_epoch_ms
from aiteam.ids import new_id
from aiteam.storage.connection import get_engine, get_session, unit_of_work
from aiteam.storage.models import SyncConflictModel, SyncOpReceiptModel
from aiteam.storage.repository import StorageRepository
from aiteam.sync import context as sync_context
from aiteam.sync import hlc as clock
from aiteam.sync.changelog import observe_hlc
from aiteam.sync.merge import SUPERSEDES, MergePlan, merge, merge_supersede, rule_for
from aiteam.sync.protocol import RESPONSE_HEADERS, SYNC_PROTOCOL
from aiteam.sync.replica import Replica
from aiteam.sync.routing import RouteMatch, RouteTable, call_app, refused
from aiteam.types import OpOutcome, SyncConflictStatus, SyncFieldConflict, SyncOperation, SyncOpResult


class OperationRefusedError(ValueError):
    """The operation itself is malformed or not one the center carries out (400)."""


class TemporaryFailureError(RuntimeError):
    """The center could not carry the operation out now; the edge retries it (503)."""


def decode_body(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return None


class _KeyedLocks:
    """One lock per key, dropped once nobody holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


class Operations:
    """Exactly-once execution of edges' operations, merged by the rules."""

    def __init__(self, app: Any, repo: StorageRepository, node_id: str) -> None:
        self._app = app
        self._repo = repo
        self._db_url = repo.db_url
        self.node_id = node_id
        self._atomic = asyncio.Lock()
        self._arrivals = _KeyedLocks()
        self._routes: RouteTable | None = None

    @property
    def routes(self) -> RouteTable:
        if self._routes is None:
            self._routes = RouteTable(self._app.routes)
        return self._routes

    async def apply(self, op: SyncOperation) -> SyncOpResult:
        match = self._validated(op)
        async with self._arrivals.hold(op.op_id):
            known = await self._receipt(op.op_id)
            if known is not None:
                return known
            if not match.surface.offline_ok:
                return await self._apply_online(op, match)
            async with self._atomic:
                return await self._apply_atomically(op, match)

    def _validated(self, op: SyncOperation) -> RouteMatch:
        if op.sync_protocol != SYNC_PROTOCOL:
            raise OperationRefusedError(f"同步协议不符：对端 {op.sync_protocol}，本节点 {SYNC_PROTOCOL}")
        if op.origin_node == self.node_id:
            raise OperationRefusedError("操作来自中心自己")
        try:
            clock.parse(op.hlc)
        except ValueError as exc:
            raise OperationRefusedError(str(exc)) from exc
        match = self.routes.match(op.method, op.path)
        if match is None or not match.shared_write:
            raise OperationRefusedError(f"{op.method} {op.path} 不是共享面的写")
        return match

    async def _receipt(self, op_id: str) -> SyncOpResult | None:
        async with get_engine(self._db_url).connect() as conn:
            row = (await conn.exec_driver_sql(
                "SELECT result FROM sync_op_receipts WHERE op_id = ?", (op_id,))).first()
        if row is None:
            return None
        return SyncOpResult.model_validate_json(row[0]).model_copy(update={"replayed": True})

    async def _apply_atomically(self, op: SyncOperation, match: RouteMatch) -> SyncOpResult:
        async def observe(session: AsyncSession) -> None:
            await session.run_sync(lambda s: observe_hlc(s.connection(), op.hlc))

        try:
            async with unit_of_work(observe, self._db_url) as session:
                plan, target = await self._plan(session, op, match)
                before = await _clocks(session, target) if target else {}
                status, headers, body = await self._execute(op, plan)
                if target and not refused(status, body):
                    await _stamp_clocks(session, op, target, before)
                return await self._settle(session, op, match, plan, status, headers, body)
        except clock.HlcDriftError as exc:
            raise TemporaryFailureError(str(exc)) from exc

    async def _apply_online(self, op: SyncOperation, match: RouteMatch) -> SyncOpResult:
        try:
            async with get_engine(self._db_url).begin() as conn:
                await conn.run_sync(lambda c: observe_hlc(c, op.hlc))
        except clock.HlcDriftError as exc:
            raise TemporaryFailureError(str(exc)) from exc
        plan = MergePlan(decode_body(op.body))
        status, headers, body = await self._execute(op, plan)
        async with get_session(self._db_url) as session:
            return await self._settle(session, op, match, plan, status, headers, body)

    async def _plan(self, session: AsyncSession, op: SyncOperation,
                    match: RouteMatch) -> tuple[MergePlan, tuple[str, str] | None]:
        body = decode_body(op.body)
        replaced = SUPERSEDES.get((op.method, match.template))
        if replaced and isinstance(body, dict) and body.get("supersedes"):
            old = await session.run_sync(lambda s: Replica(s.connection()).read(replaced, str(body["supersedes"])))
            return merge_supersede(replaced, op, body, old), None
        rule = rule_for(op.method, match.template)
        if rule is None or not isinstance(body, dict):
            return MergePlan(body), None
        pk = str(match.params.get(rule.key_param, ""))
        row = await session.run_sync(lambda s: Replica(s.connection()).read(rule.table, pk))
        return merge(rule, op, body, row, await _clocks(session, (rule.table, pk))), (rule.table, pk)

    async def _execute(self, op: SyncOperation, plan: MergePlan) -> tuple[int, dict[str, str], bytes]:
        if plan.skip:
            message = {"success": True, "data": None, "message": "同步合并：本次修改的字段均以中心较新的值为准",
                       "sync_conflicts": [c.model_dump(mode="json") for c in plan.conflicts]}
            return 200, {"content-type": "application/json"}, json.dumps(message, ensure_ascii=False).encode()
        body = json.dumps(plan.body, ensure_ascii=False).encode() if plan.merged else op.body.encode()
        wall = from_epoch_ms(clock.parse(op.hlc).wall_ms)
        with sync_context.operation(op.op_id, origin=op.origin_node, staged=False, wall=wall):
            status, headers, content = await call_app(
                self._app, op.method, op.path, query=op.query, headers=op.headers, body=body)
        if status >= 500:
            raise TemporaryFailureError(f"{op.method} {op.path} 在中心返回 {status}：{content[:300]!r}")
        return status, headers, content

    async def _settle(self, session: AsyncSession, op: SyncOperation, match: RouteMatch, plan: MergePlan,
                      status: int, headers: dict[str, str], body: bytes) -> SyncOpResult:
        conflicts = list(plan.conflicts)
        if not plan.skip and refused(status, body):
            outcome = OpOutcome.REJECTED if op.mode == "forward" else OpOutcome.CONFLICT
        else:
            outcome = OpOutcome.MERGED if plan.merged else OpOutcome.APPLIED
        for conflict in conflicts:
            self._record_conflict(session, op, conflict, SyncConflictStatus.AUTO)
        if outcome is OpOutcome.CONFLICT:
            detail = body.decode("utf-8", "replace")[:2000]
            self._record_conflict(session, op, SyncFieldConflict(
                table_name="", pk="", field="", loser=decode_body(op.body), loser_node=op.origin_node,
                rule=f"http_{status}"), SyncConflictStatus.PENDING, detail=detail)
            plan.memos.append(f"来自 {op.origin_node} 的修改未能在中心生效（{status}）：{detail[:500]}")
        await self._add_memos(match, plan.memos)
        ack_seq = (await session.execute(text("SELECT coalesce(max(seq), 0) FROM sync_changes"))).scalar_one()
        result = SyncOpResult(
            op_id=op.op_id, outcome=outcome, status_code=status,
            headers={k: v for k, v in headers.items() if k in RESPONSE_HEADERS},
            body=body.decode("utf-8", "replace"), ack_seq=ack_seq, conflicts=conflicts,
        )
        session.add(SyncOpReceiptModel(op_id=op.op_id, node=op.origin_node, hlc=op.hlc,
                                       outcome=outcome.value, result=result.model_dump_json()))
        return result

    def _record_conflict(self, session: AsyncSession, op: SyncOperation, conflict: SyncFieldConflict,
                         status: SyncConflictStatus, detail: str = "") -> None:
        session.add(SyncConflictModel(
            id=new_id(), op_id=op.op_id, loser_node=conflict.loser_node or self.node_id,
            table_name=conflict.table_name, pk=conflict.pk, field=conflict.field, winner=conflict.winner,
            loser=conflict.loser, rule=conflict.rule, winner_hlc=conflict.winner_hlc, status=status.value,
            detail=detail, method=op.method, path=op.path + (f"?{op.query}" if op.query else ""),
        ))

    async def _add_memos(self, match: RouteMatch, memos: list[str]) -> None:
        task_id = match.params.get("task_id") if match.template.startswith("/api/tasks/") else None
        if not memos or not task_id or await self._repo.get_task(str(task_id)) is None:
            return
        for memo in memos:
            await self._repo.add_task_memo(str(task_id), content=memo, author="sync", memo_type="issue")


async def _clocks(session: AsyncSession, target: tuple[str, str]) -> dict[str, tuple[str, str]]:
    rows = await session.execute(
        text("SELECT field, hlc, node FROM sync_field_clock WHERE table_name = :t AND pk = :pk"),
        {"t": target[0], "pk": target[1]})
    return {field: (hlc, node) for field, hlc, node in rows.all()}


async def _stamp_clocks(session: AsyncSession, op: SyncOperation, target: tuple[str, str],
                        before: dict[str, tuple[str, str]]) -> None:
    """Fields this operation wrote carry its clock, not the moment of the replay.

    Otherwise a second offline edit of the same field, replayed after the first,
    would lose to the first one's replay time.
    """
    for field, (hlc_now, _) in (await _clocks(session, target)).items():
        prior_hlc, prior_node = before.get(field, ("", ""))
        if prior_hlc == hlc_now:
            continue
        stamped, node = (op.hlc, op.origin_node) if op.hlc >= prior_hlc else (prior_hlc, prior_node)
        await session.execute(
            text("UPDATE sync_field_clock SET hlc = :h, node = :n WHERE table_name = :t AND pk = :pk AND field = :f"),
            {"h": stamped, "n": node, "t": target[0], "pk": target[1], "f": field})
