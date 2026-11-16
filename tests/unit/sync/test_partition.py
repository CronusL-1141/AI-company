"""Data derived from observations is computed from this node's own rows only.

The center holds an edge's usage facts and quota samples (its session face), but the
edge computed what derives from them: the projection's journal positions are numbered
in the edge's database, and two machines' samples of one account folded together
break each other's quota cycles. So the projection's reads, the sampling's starting
point and the quota summaries see only this node's partition (aiteam.storage.partition):
every row standalone, the center's own rows on the center. Rows are written with
another origin directly, as the center's intake writes them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.codex_usage_journal import CodexUsageJournalRepository
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.plan_summary_store import compute_account_summaries, fingerprint
from aiteam.storage.repository import StorageRepository
from aiteam.sync import context as sync_context
from aiteam.types import PlanUsageSnapshot

CENTER, EDGE, KEY = "mini-c0de", "air-ed9e", "a" * 64
T0 = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)


@pytest.fixture()
async def node(tmp_path: Path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'node.db'}"
    repository = StorageRepository(db_url=url)
    await repository.init_db()
    yield url, tmp_path / "node.db"
    sync_context.set_process_node("", "standalone")
    await close_db()


def _as_center() -> None:
    sync_context.set_process_node(CENTER, "center", own_origins=(CENTER,))


def _write(path: Path, sql: str, *params) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _fact(path: Path, observation: str, seq: int, origin: str, occurred: datetime) -> None:
    payload = {"observation_id": observation, "kind": "ledger"}
    _write(path, "INSERT INTO codex_usage_observations (observation_id, source_namespace, source_id, saved_at, "
                 "payload, seq, kind, occurred_at, response_id, session_id, origin_node, origin_seq) "
                 "VALUES (?, 'ns', 'src', ?, ?, ?, 'ledger', ?, ?, 's1', ?, ?)",
           observation, occurred.strftime("%Y-%m-%d %H:%M:%S.%f"), json.dumps(payload), seq,
           occurred.strftime("%Y-%m-%d %H:%M:%S.%f"), f"r-{observation}", origin, seq + 100 if origin else None)


def _plan(path: Path, snapshot_id: str, origin: str, observed: datetime) -> None:
    snapshot = PlanUsageSnapshot(
        snapshot_id=snapshot_id, account_key=KEY, limit_id="codex", used_percent=10, window_duration_ms=18_000_000,
        resets_at=observed + timedelta(hours=5), observed_at=observed, activity_observed_at=observed,
        activity_tokens=1000, activity_scope="c" * 64, source="codex_account_activity")
    _write(path, "INSERT INTO account_plan_snapshots (id, account_key, observed_at, payload, origin_node) "
                 "VALUES (?, ?, ?, ?, ?)", snapshot_id, KEY, observed.strftime("%Y-%m-%d %H:%M:%S.%f"),
           snapshot.model_dump_json(), origin)


async def test_the_usage_projection_reads_only_this_nodes_facts(node) -> None:
    url, path = node
    _fact(path, "own", 1, CENTER, T0)
    _fact(path, "edge", 2, EDGE, T0 + timedelta(minutes=1))
    journal = CodexUsageJournalRepository(url)
    assert await journal.recorded_through() == 2  # standalone: every fact
    _as_center()
    assert await journal.recorded_through() == 1
    window = dict(after=0, through=10, since=T0 - timedelta(hours=1), until=T0 + timedelta(hours=1),
                  binding=T0 - timedelta(hours=2))
    assert [row[0] for row in await journal.consumable(**window)] == [1]
    assert [row[0] for row in await journal.ledger_occurrences(["r-own", "r-edge"], through=10)] == [1]
    assert await journal.ledger_sessions(["s1"], through=10) == {"s1"}
    assert [row[0] for row in await journal.session_facts(["s1"], kind="ledger", through=10)] == [1]


async def test_sampling_continues_from_this_nodes_latest_snapshot(node) -> None:
    url, path = node
    _plan(path, "own-older", CENTER, T0)
    _plan(path, "edge-newer", EDGE, T0 + timedelta(minutes=5))
    accounts = AccountUsageRepository(url)
    assert (await accounts.latest_plan_snapshot(KEY)).snapshot_id == "edge-newer"
    _as_center()
    assert (await accounts.latest_plan_snapshot(KEY)).snapshot_id == "own-older"


async def test_quota_summaries_fold_this_nodes_rows_only(node) -> None:
    url, path = node
    _plan(path, "own", CENTER, T0)
    _plan(path, "edge", EDGE, T0 + timedelta(minutes=5))
    _as_center()
    async with get_session(url) as session:
        assert await fingerprint(session, "plan", KEY) == (1, "own")
    con = sqlite3.connect(path, isolation_level=None)
    try:
        assert compute_account_summaries(con, KEY, [CENTER])["fingerprints"]["plan"] == [1, "own"]
        assert compute_account_summaries(con, KEY)["fingerprints"]["plan"] == [2, "edge"]
    finally:
        con.close()


async def test_other_nodes_quota_is_shown_as_their_latest_observation(node) -> None:
    url, path = node
    _plan(path, "own", CENTER, T0)
    _plan(path, "edge-older", EDGE, T0 + timedelta(minutes=1))
    _plan(path, "edge-latest", EDGE, T0 + timedelta(minutes=2))
    accounts = AccountUsageRepository(url)
    assert await accounts.latest_plan_snapshots_elsewhere() == []  # standalone: nothing is elsewhere
    _as_center()
    assert [(origin, s.snapshot_id) for origin, s in await accounts.latest_plan_snapshots_elsewhere()] == [
        (EDGE, "edge-latest")]


async def test_deleting_a_project_on_the_center_deletes_only_its_own_session_rows(node) -> None:
    """The edge deletes its own teams of the project when the deletion reaches it; the
    center deleting them too would make it a second writer of the edge's rows."""
    url, path = node
    repository = StorageRepository(db_url=url)
    project = await repository.create_project(name="doomed", root_path="/work/doomed")
    for team_id, origin in (("t-center", CENTER), ("t-edge", EDGE), ("t-baseline", "")):
        _write(path, "INSERT INTO teams (id, name, mode, status, summary, config, created_at, updated_at, "
                     "project_id, origin_node) VALUES (?, ?, 'coordinate', 'active', '', '{}', '2026-11-15', "
                     "'2026-11-15', ?, ?)", team_id, f"session-{team_id}", project.id, origin)
        _write(path, "INSERT INTO events (id, type, source, data, timestamp, entity_id, origin_node) VALUES "
                     "(?, 'team.created', 'test', '{}', '2026-11-15', ?, ?)", f"e-{team_id}", team_id, origin)
    _as_center()
    assert await repository.delete_project(project.id)
    con = sqlite3.connect(path)
    try:
        assert sorted(con.execute("SELECT id FROM teams").fetchall()) == [("t-baseline",), ("t-edge",)]
        assert sorted(con.execute("SELECT id FROM events").fetchall()) == [("e-t-baseline",), ("e-t-edge",)]
    finally:
        con.close()
