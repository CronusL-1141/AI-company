"""This node's part in the sync, read for the health check and the operator.

Red (``healthy`` false) when something needs a person: an identity that did not hold
on this machine (the node runs held, unbound or standalone; aiteam.sync.node),
an edge whose replica was written outside any operation (the replica is never the
authority), a handshake the center failed, a queue whose head keeps failing or that
has waited too long, a session face that has not gone up for too long, a worker
that died, conflicts no rule could decide, or session rows the center refused
(they claimed an origin their batch did not come from).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.engine import Connection

from aiteam.clock import from_epoch_ms, parse_utc, utc_now
from aiteam.storage.connection import get_engine
from aiteam.sync import context as sync_context
from aiteam.sync import hlc as clock
from aiteam.sync import ledger, upload
from aiteam.sync.edge import active_edge
from aiteam.sync.protocol import BACKLOG_ALERT_S, STUCK_ATTEMPTS
from aiteam.sync.tables import SHARED_TABLES_SQL
from aiteam.types import LinkState, NodeMode, NodeRole, NodeStatus, SyncStatus

if TYPE_CHECKING:
    from aiteam.sync.center import Center

# Session changes still to go up are counted this far: a number to look at, not a total.
_COUNTED_UP_TO = 100_000



# How a node whose identity did not hold at startup runs (aiteam.sync.node.decide).
_IDENTITY_PROBLEM = {
    NodeMode.HELD: "节点身份未核对通过，同步通道已停（本机的行仍按登记的节点记录）：",
    NodeMode.UNBOUND: "本库属于另一台机器的节点，本机以临时来源记录、不同步：",
    NodeMode.STANDALONE: "节点身份被拒，已按单机运行，写入不会到达中心：",
    NodeMode.SYNCING: "",
}


async def read(node: NodeStatus, db_url: str, center: Center | None) -> SyncStatus:
    origins = sync_context.own_origins()
    status = SyncStatus(role=node.role, node_id=node.node_id, refusal=node.refusal,
                        own_origins=list(origins) if origins is not None else None)
    if node.refusal:
        status.problems.append(_IDENTITY_PROBLEM[node.mode] + node.refusal)
    if node.role is NodeRole.EDGE:
        await _edge(status, db_url)
    elif node.role is NodeRole.CENTER and center is not None:
        await _center(status, db_url, center)
    status.healthy = not status.problems
    return status


async def _edge(status: SyncStatus, db_url: str) -> None:
    def read(conn: Connection) -> tuple[ledger.Peer, ledger.Backlog, int, int, int, str | None]:
        tentative = conn.exec_driver_sql("SELECT count(*) FROM sync_tentative").scalar_one()
        orphans = conn.exec_driver_sql(
            f"SELECT count(*) FROM sync_changes WHERE table_name IN ({SHARED_TABLES_SQL})").scalar_one()
        peer = ledger.read_peer(conn)
        behind = upload.backlog(conn, peer.up_cursor, _COUNTED_UP_TO)
        return peer, ledger.backlog(conn), tentative, orphans, behind, upload.oldest_pending_hlc(conn, peer.up_cursor)

    async with get_engine(db_url).connect() as conn:
        peer, queue, status.tentative_rows, status.orphan_writes, status.upload_backlog, oldest = \
            await conn.run_sync(read)
    status.upload_cursor = peer.up_cursor
    status.upload_oldest_at = from_epoch_ms(clock.parse(oldest).wall_ms) if oldest else None
    edge = active_edge()
    status.center_url = edge.center_url if edge is not None else ""
    status.center_node_id, status.link, status.cursor = peer.center_node_id, peer.link, peer.cursor
    status.handshake_error, status.last_error = peer.handshake_error, peer.last_error
    status.pending_ops, status.head_attempts = queue.pending, queue.head_attempts
    status.oldest_pending_at = parse_utc(queue.oldest_at)
    if status.orphan_writes:
        status.problems.append(
            f"本机副本有 {status.orphan_writes} 处共享面写入不在任何操作里（孤儿写入）：副本不是正本，"
            "这些写入不会到达中心；用 python3 -m aiteam.sync orphans 查看与处理")
    if status.handshake_error:
        status.problems.append(f"与中心握手失败：{status.handshake_error}")
    status.problems += _queue_problems(queue, status.oldest_pending_at)
    if (status.link is LinkState.ATTACHED and status.upload_oldest_at is not None
            and (utc_now() - status.upload_oldest_at).total_seconds() >= BACKLOG_ALERT_S):
        status.problems.append(f"本机会话面有变更超过 {int(BACKLOG_ALERT_S // 60)} 分钟未上行到中心"
                               f"（最早 {status.upload_oldest_at.isoformat()}）：{peer.last_error or '原因未记录'}")
    if edge is not None and not edge.worker_alive:
        status.problems.append("同步子进程未在运行")


def _queue_problems(queue: ledger.Backlog, oldest: datetime | None) -> list[str]:
    """A queue whose head keeps failing, or that has waited too long, needs a person."""
    if not queue.pending:
        return []
    problems = []
    if queue.head_attempts >= STUCK_ATTEMPTS:
        problems.append(f"队首操作已重试 {queue.head_attempts} 次仍未送达中心：{queue.head_error or '原因未记录'}")
    if oldest is not None and (utc_now() - oldest).total_seconds() >= BACKLOG_ALERT_S:
        problems.append(f"有 {queue.pending} 条写入积压超过 {int(BACKLOG_ALERT_S // 60)} 分钟未送达中心"
                        f"（最早 {oldest.isoformat()}）")
    return problems


async def _center(status: SyncStatus, db_url: str, center: Center) -> None:
    async with get_engine(db_url).connect() as conn:
        status.pending_conflicts = (await conn.exec_driver_sql(
            "SELECT count(*) FROM sync_conflicts WHERE status = 'pending'")).scalar_one()
    status.wakes_awaiting_claim = await center.wakes.awaiting_claim()
    status.intakes = await center.intake.statuses()
    if status.pending_conflicts:
        status.problems.append(f"有 {status.pending_conflicts} 处同步冲突待用户处理（GET /api/sync/conflicts）")
    for intake in status.intakes:
        if intake.refused:
            status.problems.append(f"{intake.node} 上行的会话面有 {intake.refused} 行声称来自别的节点，中心未收"
                                   "（边缘库可能被别处的数据混入，需人工查看）")
