"""The ledger is session face: an edge's rows go up to the center and are kept there.

Rows of one text in several states (refused, failed, sent) share a content hash;
only a live one holds the claim. The center's intake treats a partial unique index
as unique where the value is not empty, which is exactly the claim column: the rows
that do not hold it are all taken in. A live row of the same text the center already
holds for another node (a duplicate across machines) is not taken; it stays on its
edge (aiteam.storage.models.FeedbackSubmissionModel).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from aiteam.storage.connection import close_db
from aiteam.storage.feedback import FeedbackRepository
from aiteam.storage.models import Base
from aiteam.storage.repository import StorageRepository
from aiteam.sync import context as sync_context
from aiteam.sync import ledger, upload
from aiteam.sync.intake import Intake
from aiteam.sync.tables import SESSION_TABLES, schema_problems
from aiteam.types import FeedbackStatus, SyncIntakeOutcome

CENTER, EDGE = "mini-c0de", "air-ed9e"
HASH = "a" * 64


def _sql(path: Path, query: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(query).fetchall()
        connection.commit()
        return rows
    finally:
        connection.close()


async def _database(path: Path, node: str, capture: int) -> StorageRepository:
    repository = StorageRepository(db_url=f"sqlite+aiosqlite:///{path}")
    await repository.init_db()
    _sql(path, f"UPDATE sync_state SET capture = {capture}, node_id = '{node}'")
    return repository


async def _rows(repository: StorageRepository, node: str, role: str, *statuses: FeedbackStatus) -> None:
    sync_context.set_process_node(node, role)
    try:
        for n, status in enumerate(statuses):
            await FeedbackRepository(repository.db_url).insert(
                id=f"{node}-{n}", kind="bug", mode="new", target_repo="acme/os", content_hash=HASH, status=status)
    finally:
        sync_context.set_process_node("", "standalone")


def test_the_ledger_is_grouped_on_the_session_face() -> None:
    assert "feedback_submissions" in SESSION_TABLES
    assert schema_problems(Base.metadata.tables) == []


@pytest.fixture()
def paths(tmp_path: Path):
    yield tmp_path / "edge.db", tmp_path / "center.db"
    sync_context.set_baseline_owner("")


async def test_an_edges_rows_reach_the_center_beside_the_centers_own(paths) -> None:
    edge_path, center_path = paths
    edge = await _database(edge_path, EDGE, 3)
    await _rows(edge, EDGE, "edge", FeedbackStatus.REFUSED, FeedbackStatus.FAILED, FeedbackStatus.SENT)
    engine = create_engine(f"sqlite:///{edge_path}")
    try:
        with engine.begin() as conn:
            ledger.ensure_peer(conn)
        with engine.connect() as conn:
            batch = upload.read_batch(conn, EDGE, 0)
    finally:
        engine.dispose()
    assert sorted(c.pk for c in batch.changes if c.table_name == "feedback_submissions") == [
        f"{EDGE}-0", f"{EDGE}-1", f"{EDGE}-2"]

    center = await _database(center_path, CENTER, 1)
    await _rows(center, CENTER, "center", FeedbackStatus.EXPIRED)
    ack = await Intake(center.db_url, CENTER).apply(batch)
    assert (ack.outcome, ack.applied, ack.skipped_foreign) == (SyncIntakeOutcome.APPLIED, 3, 0)
    assert _sql(center_path, "SELECT id, origin_node, status, claim_hash != '' FROM feedback_submissions "
                             "ORDER BY id") == [
        (f"{EDGE}-0", EDGE, "refused", 0), (f"{EDGE}-1", EDGE, "failed", 0), (f"{EDGE}-2", EDGE, "sent", 1),
        (f"{CENTER}-0", CENTER, "expired", 0)]
    await close_db()
