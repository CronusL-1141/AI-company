"""This node's part in the sync, read for the health check and the operator.

Red (``healthy`` false) when something needs a person: an identity refused on this
machine (the node fell back to standalone, its writes no longer reach the center),
an edge whose replica was written outside any operation (the replica is never the
authority), a handshake the center failed, a queue whose head keeps failing or that
has waited too long, a worker that died, or conflicts no rule could decide.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.engine import Connection

from aiteam.clock import parse_utc, utc_now
from aiteam.storage.connection import get_engine
from aiteam.sync import ledger
from aiteam.sync.edge import active_edge
from aiteam.sync.protocol import BACKLOG_ALERT_S, STUCK_ATTEMPTS
from aiteam.sync.tables import SHARED_TABLES_SQL
from aiteam.types import NodeRole, NodeStatus, SyncStatus

if TYPE_CHECKING:
    from aiteam.sync.center import Center



async def read(node: NodeStatus, db_url: str, center: Center | None) -> SyncStatus:
    status = SyncStatus(role=node.role, node_id=node.node_id, refusal=node.refusal)
    if node.refusal:
        status.problems.append(f"节点身份被拒，已按 standalone 运行，共享面写入不再到达中心：{node.refusal}")
    if node.role is NodeRole.EDGE:
        await _edge(status, db_url)
    elif node.role is NodeRole.CENTER and center is not None:
        await _center(status, db_url, center)
    status.healthy = not status.problems
    return status


async def _edge(status: SyncStatus, db_url: str) -> None:
    def read(conn: Connection) -> tuple[ledger.Peer, ledger.Backlog, int, int]:
        tentative = conn.exec_driver_sql("SELECT count(*) FROM sync_tentative").scalar_one()
        orphans = conn.exec_driver_sql(
            f"SELECT count(*) FROM sync_changes WHERE table_name IN ({SHARED_TABLES_SQL})").scalar_one()
        return ledger.read_peer(conn), ledger.backlog(conn), tentative, orphans

    async with get_engine(db_url).connect() as conn:
        peer, queue, status.tentative_rows, status.orphan_writes = await conn.run_sync(read)
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
    if status.pending_conflicts:
        status.problems.append(f"有 {status.pending_conflicts} 处同步冲突待用户处理（GET /api/sync/conflicts）")
