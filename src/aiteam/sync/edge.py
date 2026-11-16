"""The edge's side of the sync: every shared-face write becomes an operation.

A write request to a shared-face route (aiteam.sync.routing)
is recorded in ``sync_outbox`` first, under a fresh operation id and clock. Then:

* attached, nothing queued before it (operations other requests are forwarding
  right now are not a queue), and this node's session face up to the operation
  fitting in one forward: forwarded to the center with that session tail, whose
  answer is the answer; its session effects (events, agents) are written here; the
  request returns once the replica has caught up with it (at most
  ``READ_YOUR_WRITE_WAIT_S``), so a read right after sees the write;
* otherwise (offline, draining a backlog, the forward failed or timed out):
  staged. The route runs here, in one unit of work under the operation's ids,
  without events; the shared rows it changed are listed in ``sync_tentative`` and
  the operation stays queued for the worker to replay. A timed-out forward may
  have reached the center: the replay is deduplicated there by operation id. An
  answer that is not the center's verdict (the peer is no center, runs other code)
  detaches the edge at once.
* writes only allowed online (deletions, the ecosystem archive) are forwarded with
  a longer timeout, also wait for the replica, and are refused with 503 while the
  edge is offline or has a queue they would overtake; they are never queued.

Reads are always served from the local replica. The worker subprocess
(aiteam.sync.worker) keeps the link state, replays the queue, keeps the replica
current and settles staged rows; this process nudges it through its stdin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from aiteam.clock import utc_now
from aiteam.ids import time_ordered_id
from aiteam.storage.connection import get_engine, unit_of_work
from aiteam.sync import context as sync_context
from aiteam.sync import effects as session_effects
from aiteam.sync import ledger, upload
from aiteam.sync.changelog import latest_seq, next_hlc, raise_floor
from aiteam.sync.merge import rule_for
from aiteam.sync.protocol import (
    FORWARD_TAIL_ROWS,
    FORWARD_TIMEOUT_S,
    ONLINE_ONLY_TIMEOUT_S,
    READ_YOUR_WRITE_WAIT_S,
    STAGED_HEADER,
    SYNC_PROTOCOL,
    operation_headers,
    timing,
)
from aiteam.sync.routing import WRITE_METHODS, RouteMatch, RouteTable, call_app, refused, writes
from aiteam.sync.tables import SHARED_TABLES_SQL
from aiteam.types import LinkState, ScheduledTask, SyncOperation, SyncOpResult, SyncSessionBatch

logger = logging.getLogger(__name__)

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


_active: EdgeSync | None = None


def active_edge() -> EdgeSync | None:
    return _active


class _Unreachable(Exception):  # noqa: N818 - control flow: the forward never got there
    pass


class _Declined(Exception):  # noqa: N818 - control flow: the center said it did not carry it out now
    pass


class _StageRefused(Exception):  # noqa: N818 - control flow: the route refused locally
    pass


async def _read_body(receive: Receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _replay(body: bytes) -> Receive:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            await asyncio.Event().wait()  # like a client that keeps the connection open
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _respond(send: Send, status: int, headers: dict[str, str], body: bytes) -> None:
    raw = [(name.encode("latin-1"), value.encode("latin-1")) for name, value in headers.items()]
    raw.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


def _detail(response: httpx.Response) -> str:
    """The reason a center gave with an error answer."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return str(detail) if detail else response.text[:300]


def _refusal(status: int, message: str) -> tuple[int, dict[str, str], bytes]:
    payload = {"success": False, "error": message, "detail": message}
    return status, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode()


class EdgeSync:
    """The shared-face writes of an edge node and the worker that keeps it in sync."""

    def __init__(self, app: Any, db_url: str, node_id: str, center_url: str, db_path: str, *,
                 channel: bool = True) -> None:
        self._app = app
        # No channel: the node's identity did not hold at startup (aiteam.sync.node,
        # held). No worker runs; every shared-face write is staged for the replay that
        # follows once the identity holds again.
        self.channel = channel
        self._db_url = db_url
        self._db_path = db_path
        self.node_id = node_id
        self.center_url = center_url
        self._routes: RouteTable | None = None
        self._client = httpx.AsyncClient(base_url=center_url, trust_env=False)
        self._worker: asyncio.subprocess.Process | None = None
        # Operations of requests this process is handling right now (recorded, not yet
        # settled or staged): concurrent with each other, not a queue to wait behind.
        self._in_flight: set[str] = set()

    async def _ledger(self, work: Callable[..., Any], *args: Any, write: bool = False) -> Any:
        """Run one ledger function (aiteam.sync.ledger) on this node's database."""
        engine = get_engine(self._db_url)
        async with (engine.begin() if write else engine.connect()) as conn:
            return await conn.run_sync(work, *args)

    @property
    def routes(self) -> RouteTable:
        if self._routes is None:
            self._routes = RouteTable(self._app.routes)
        return self._routes

    # -- lifecycle -----------------------------------------------------------

    async def start(self, stopped_because: str = "") -> None:
        global _active  # noqa: PLW0603 - one edge per process
        await self._ledger(ledger.ensure_peer, write=True)
        _active = self
        if self.channel:
            await self._spawn_worker()
        else:
            await self._ledger(ledger.detach, f"同步通道已停：{stopped_because}", write=True)

    async def stop(self) -> None:
        global _active  # noqa: PLW0603
        if _active is self:
            _active = None
        worker, self._worker = self._worker, None
        if worker is not None and worker.returncode is None:
            if worker.stdin is not None:
                worker.stdin.close()
            try:
                await asyncio.wait_for(worker.wait(), timeout=5)
            except TimeoutError:
                worker.kill()
                await worker.wait()
        await self._client.aclose()

    async def _spawn_worker(self) -> None:
        self._worker = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "aiteam.sync.worker", "--db", self._db_path,
            "--node-id", self.node_id, "--center-url", self.center_url,
            stdin=asyncio.subprocess.PIPE,
        )

    async def nudge(self) -> None:
        """Wake the worker now (it otherwise looks every pull interval); restart it if it died."""
        worker = self._worker
        if worker is None:
            return
        if worker.returncode is not None:
            logger.warning("Multi-host sync: the worker exited (%s), restarting it", worker.returncode)
            await self._spawn_worker()
            worker = self._worker
        try:
            worker.stdin.write(b"\n")  # type: ignore[union-attr]
            await worker.stdin.drain()  # type: ignore[union-attr]
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def peer(self) -> ledger.Peer:
        return await self._ledger(ledger.read_peer)

    @property
    def worker_alive(self) -> bool:
        return self._worker is not None and self._worker.returncode is None

    # -- the middleware's entry ----------------------------------------------

    async def dispatch(self, scope: Scope, receive: Receive, send: Send, inner: ASGIApp) -> None:
        method, path = scope["method"], scope["path"]
        match = self.routes.match(method, path)
        if match is None or not match.shared_write:
            await inner(scope, receive, send)
            return
        body = await _read_body(receive)
        if not writes(match, method, body):
            await inner(scope, _replay(body), send)
            return
        op = SyncOperation(
            op_id=time_ordered_id(), origin_node=self.node_id, hlc="", mode="replay", method=method, path=path,
            query=scope.get("query_string", b"").decode("latin-1"), body=body.decode("utf-8", "replace"),
            headers=operation_headers(scope.get("headers", [])), base=await self._base(method, match),
            sync_protocol=SYNC_PROTOCOL,
        )
        if not match.surface.offline_ok:
            await _respond(send, *await self._online_only(op))
            return
        op = await self._ledger(ledger.record, op, write=True)
        self._in_flight.add(op.op_id)
        try:
            result, sent = None, False
            carried = await self._forwardable(op)
            if carried is not None:
                sent = True
                try:
                    result = await self._forward(carried, timing("forward_timeout", FORWARD_TIMEOUT_S))
                except _Unreachable:
                    sent = False
                except _Declined:
                    pass  # staged, replayed later
            if result is not None:
                await self._ledger(ledger.settle, op.op_id, result, write=True)
        finally:
            self._in_flight.discard(op.op_id)
        if result is not None:
            await self._await_replica(result.ack_seq)
            await _respond(send, result.status_code, result.headers, result.body.encode())
            return
        await self._stage(scope, body, op, inner, send, sent=sent)
        await self.nudge()

    async def _base(self, method: str, match: RouteMatch) -> dict[str, Any]:
        rule = rule_for(method, match.template)
        if rule is None:
            return {}
        return await self._ledger(ledger.base_for, rule, str(match.params.get(rule.key_param, "")))

    async def _online_only(self, op: SyncOperation) -> tuple[int, dict[str, str], bytes]:
        peer = await self._ledger(ledger.read_peer)
        if peer.link is not LinkState.ATTACHED:
            return _refusal(503, "这项写入只能在连上中心节点时进行（多机同步：删除与生态档案只在中心执行）")
        if (await self._ledger(ledger.backlog, None, frozenset(self._in_flight))).pending:
            return _refusal(503, "本机还有未送达中心的写入在排队，这项只许在线的写不能越过它们，请稍后再试")
        op = op.model_copy(update={"hlc": await self._ledger(next_hlc, write=True),
                                   "session_seq": await self._ledger(latest_seq)})
        carried = await self._with_session_tail(op)
        if carried is None:
            return _refusal(503, "本机的会话数据还在上行到中心，这项只许在线的写请稍后再试")
        try:
            result = await self._forward(carried, timing("online_only_timeout", ONLINE_ONLY_TIMEOUT_S))
        except _Unreachable:
            return _refusal(503, "中心节点不可达，这项写入只能在线进行")
        except _Declined as exc:
            return _refusal(503, f"中心节点这次没有执行这项写入，请稍后再试：{exc}")
        if result is None:
            return _refusal(504, "中心节点未在时限内答复，这项写入的结果未知，请稍后查看")
        await self._ledger(_apply_effects, result, write=True)
        await self._await_replica(result.ack_seq)
        return result.status_code, result.headers, result.body.encode()

    async def _forwardable(self, op: SyncOperation) -> SyncOperation | None:
        """The operation to forward, carrying its session tail; None when it is to be staged.

        Attached, no earlier operation still queued (order is never overtaken), and the
        session face it waits for fits in one forward. Operations other requests of
        this process are forwarding at this moment are concurrent with this one, not
        ahead of it in a queue.
        """
        peer = await self._ledger(ledger.read_peer)
        queued = await self._ledger(ledger.backlog, op.hlc, frozenset(self._in_flight))
        if peer.link is not LinkState.ATTACHED or queued.pending:
            return None
        return await self._with_session_tail(op, peer)

    async def _with_session_tail(self, op: SyncOperation, peer: ledger.Peer | None = None) -> SyncOperation | None:
        """``op`` carrying this node's session face up to its ``session_seq`` (nothing when the
        center has it already); None when that is more than one forward carries."""
        peer = peer or await self._ledger(ledger.read_peer)
        if peer.up_cursor >= op.session_seq:
            return op
        tail = await self._ledger(_read_tail, self.node_id, peer.up_cursor, op.session_seq)
        return op.model_copy(update={"session_tail": tail}) if tail.upto >= op.session_seq else None

    async def _forward(self, op: SyncOperation, timeout: float) -> SyncOpResult | None:
        """The center's verdict; None when the answer is unknown (no reply in time).

        Raises _Unreachable when the operation certainly did not arrive, _Declined when
        the center answered that it did not carry it out now (503). An answer other
        than a verdict means the peer is not (now) a center this edge can sync with:
        the edge detaches at once and the worker's handshake finds out why.
        """
        try:
            response = await self._client.post(
                "/api/sync/ops", content=op.model_copy(update={"mode": "forward"}).model_dump_json(),
                headers={"content-type": "application/json"}, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            await self._ledger(ledger.detach, f"转发失败（未连上中心）：{exc}", write=True)
            raise _Unreachable from exc
        except httpx.HTTPError as exc:
            logger.warning("Multi-host sync: forward of %s has no answer (%s); staged for replay", op.op_id, exc)
            return None
        if response.status_code == 503:
            raise _Declined(_detail(response))
        if response.status_code != 200:
            reason = f"中心对转发的应答不是裁决（{response.status_code}）：{response.text[:300]}"
            await self._ledger(ledger.detach, reason, write=True)
            raise _Unreachable
        return SyncOpResult.model_validate_json(response.content)

    async def _await_replica(self, ack_seq: int) -> None:
        await self.nudge()
        deadline = asyncio.get_running_loop().time() + timing("read_your_write_wait", READ_YOUR_WRITE_WAIT_S)
        while (await self._ledger(ledger.read_peer)).cursor < ack_seq:
            if asyncio.get_running_loop().time() >= deadline:
                return
            await asyncio.sleep(0.05)

    # -- staging -------------------------------------------------------------

    async def _stage(self, scope: Scope, body: bytes, op: SyncOperation, inner: ASGIApp, send: Send,
                     *, sent: bool) -> None:
        """Carry the write out here, as a stand-in the center's version will replace."""
        start_seq = 0

        async def first_write(session: AsyncSession) -> None:
            nonlocal start_seq
            await session.execute(text("UPDATE sync_outbox SET staged = 1, updated_at = :now WHERE op_id = :op"),
                                  {"now": utc_now().isoformat(), "op": op.op_id})
            start_seq = (await session.execute(text("SELECT coalesce(max(seq), 0) FROM sync_changes"))).scalar_one()

        messages: list[dict[str, Any]] = []

        async def capture(message: dict[str, Any]) -> None:
            messages.append(message)

        try:
            async with unit_of_work(first_write, self._db_url) as session:
                with sync_context.operation(op.op_id, origin=self.node_id, staged=True):
                    await inner(scope, _replay(body), capture)
                if refused(_status(messages), _body(messages)):
                    raise _StageRefused
                await session.execute(text(
                    "INSERT INTO sync_tentative (table_name, pk, op_id) SELECT DISTINCT table_name, pk, :op "
                    f"FROM sync_changes WHERE seq > :s AND table_name IN ({SHARED_TABLES_SQL}) "
                    "ON CONFLICT DO NOTHING"), {"op": op.op_id, "s": start_seq})
                await session.run_sync(lambda s: raise_floor(s.connection()))
                await session.execute(text(
                    f"DELETE FROM sync_changes WHERE seq > :s AND table_name IN ({SHARED_TABLES_SQL})"),
                    {"s": start_seq})
        except _StageRefused:
            await self._drop(op.op_id, sent, f"本机暂存时被拒（{_status(messages)}）")
        except BaseException:
            await self._drop(op.op_id, sent, "本机暂存时出错")
            raise
        staged = not refused(_status(messages), _body(messages))
        for message in messages:
            if message["type"] == "http.response.start" and staged:
                message = {**message, "headers": [*message.get("headers", []), (STAGED_HEADER.encode(), b"staged")]}
            await send(message)

    async def _drop(self, op_id: str, sent: bool, reason: str) -> None:
        """A write that failed here before reaching the center is never replayed.

        One whose forward may have reached it stays queued: the center deduplicates it.
        """
        if not sent:
            await self._ledger(ledger.drop, op_id, reason, write=True)

    # -- for governance and the reaper ----------------------------------------

    async def submit(self, method: str, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        """A shared-face write of this node's own governance, made like any other."""
        if method not in WRITE_METHODS:
            raise ValueError(method)
        status, _, body = await call_app(
            self._app, method, path, headers={"content-type": "application/json"},
            body=json.dumps(payload, ensure_ascii=False).encode())
        try:
            return status, json.loads(body or b"null")
        except ValueError:
            return status, body.decode("utf-8", "replace")

    async def claim_wakes(self) -> list[ScheduledTask]:
        """Due wakes of this node's sessions, handed over by the center."""
        if (await self._ledger(ledger.read_peer)).link is not LinkState.ATTACHED:
            return []
        try:
            response = await self._client.post(
                "/api/sync/wakes/claim", json={"node_id": self.node_id},
                timeout=timing("forward_timeout", FORWARD_TIMEOUT_S))
        except httpx.HTTPError as exc:
            logger.info("Multi-host sync: wake claim failed: %s", exc)
            return []
        if response.status_code != 200:
            return []
        return [ScheduledTask.model_validate(item) for item in response.json().get("claimed", [])]


def _read_tail(conn: Connection, origin: str, after: int, upto: int) -> SyncSessionBatch:
    return upload.read_batch(conn, origin, after, upto=upto, max_rows=FORWARD_TAIL_ROWS)


def _apply_effects(conn: Connection, result: SyncOpResult) -> None:
    """The session effects of a write never queued (only allowed online): it was not staged."""
    session_effects.apply(conn, result.effects, staged=False)


def _body(messages: list[dict[str, Any]]) -> bytes:
    return b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")


def _status(messages: list[dict[str, Any]]) -> int:
    for message in messages:
        if message["type"] == "http.response.start":
            return int(message["status"])
    return 500


class EdgeSyncMiddleware:
    """Route shared-face writes through the edge's sync; inert on any other node."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        edge = _active
        if edge is None or scope["type"] != "http" or scope["method"] not in WRITE_METHODS:
            await self.app(scope, receive, send)
            return
        await edge.dispatch(scope, receive, send, self.app)

