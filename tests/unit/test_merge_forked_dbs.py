"""The one-time merge of two forked OS databases (scripts/merge_forked_dbs.py).

One snapshot (built with the real init_db and ORM rows) is copied into an edge and a
center database, each changes its copy after the fork, and the script merges them.
Every assertion reads the merged database through a fresh connection: what counts is
what was written, not what the merge said it would write.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aiteam.storage.connection import close_db
from aiteam.storage.models import (
    AccountPlanSnapshotModel,
    AccountPlanSummaryModel,
    AgentActivityModel,
    AgentModel,
    CodexUsageObservationModel,
    CodexUsageSourceCursorModel,
    EventModel,
    GovernanceLeaseModel,
    MeetingModel,
    MemoryModel,
    ProjectModel,
    ReportModel,
    TaskMemoModel,
    TaskModel,
    TeamModel,
)
from aiteam.storage.repository import StorageRepository
from aiteam.sync.merge import STATUS_RANKS
from aiteam.sync.tables import LOCAL_TABLES, SHARED_TABLES
from aiteam.types import NodeIdentity, NodeRole

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("merge_forked_dbs", ROOT / "scripts" / "merge_forked_dbs.py")
assert _spec and _spec.loader
mf = importlib.util.module_from_spec(_spec)
sys.modules["merge_forked_dbs"] = mf  # dataclasses look their module up while being defined
_spec.loader.exec_module(mf)

CENTER_NODE = "mini-3f9c"
PROJECT = "11111111-0000-0000-0000-000000000001"
GONE, KEPT = "22222222-0000-0000-0000-000000000001", "22222222-0000-0000-0000-000000000002"


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, 15, hour, minute, second, tzinfo=UTC)


FORK = at(9, 1, 24)


def _put(db: Path, *rows: object) -> None:
    engine = create_engine(f"sqlite:///{db}")
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
    engine.dispose()


def _sql(db: Path, statement: str, params: tuple = ()) -> None:
    con = sqlite3.connect(db)
    con.execute(statement, params)
    con.commit()
    con.close()


def _task(task_id: str, *, status: str = "pending", revision: int = 1, created_at: datetime = at(8),
          project: str = PROJECT) -> TaskModel:
    return TaskModel(id=task_id, title="原标题", status=status, project_id=project, created_at=created_at,
                     config={"task_management": {"revision": revision}})


def _observation(seq: int, saved: datetime, source: str) -> CodexUsageObservationModel:
    return CodexUsageObservationModel(observation_id=f"obs-{source}-{seq}", source_namespace="ns", source_id=source,
                                      saved_at=saved, payload={"n": seq}, seq=seq, kind="ledger")


def _snapshot(snapshot_id: str, observed: datetime, position: int) -> AccountPlanSnapshotModel:
    return AccountPlanSnapshotModel(id=snapshot_id, account_key="a" * 64, observed_at=observed,
                                    payload={"snapshot_id": snapshot_id, "activity_journal_seq": position})


@pytest.fixture(scope="module")
def snapshot_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The database both machines started from."""
    path = tmp_path_factory.mktemp("snapshot") / "aiteam.db"

    async def build() -> None:
        await StorageRepository(db_url=f"sqlite+aiosqlite:///{path}").init_db()
        await close_db()

    asyncio.run(build())
    _put(path,
         ProjectModel(id=PROJECT, name="p", root_path="/p", created_at=at(8), updated_at=at(8)),
         _task("t-conflict", revision=3), _task("t-done", status="running"),
         TaskMemoModel(id="memo-base", task_id="t-conflict", project_id=PROJECT, content="基线", created_at=at(8)),
         MemoryModel(id="mem-keep", scope="project", scope_id=PROJECT, content="留着", created_at=at(8)),
         MemoryModel(id="mem-gone", scope="project", scope_id=PROJECT, content="中心删了", created_at=at(8)),
         MemoryModel(id="mem-revived", scope="project", scope_id=PROJECT, content="边缘删了", created_at=at(8)),
         MemoryModel(id="mem-twice", scope="project", scope_id=PROJECT, content="两边都失效", created_at=at(8)),
         MeetingModel(id="meet", team_id="team-air", topic="t", status="active", participants=["a"],
                      project_id=PROJECT, created_at=at(8)),
         *[row for project in (GONE, KEPT) for row in (
             ProjectModel(id=project, name=project[-1], root_path=f"/{project}", created_at=at(8), updated_at=at(8)),
             _task(f"task-{project[-1]}", project=project),
             ReportModel(id=f"report-{project[-1]}", project_id=project, topic="t", created_at=at(8)))],
         ReportModel(id="report-gone", project_id=PROJECT, topic="中心删了", created_at=at(8)),
         TeamModel(id="team-air", name="session-aaaa", status="active", created_at=at(8), updated_at=at(8)),
         TeamModel(id="team-resumed", name="session-bbbb", status="active", created_at=at(8), updated_at=at(8)),
         AgentModel(id="agent-air", team_id="team-air", name="a", role="leader", status="busy",
                    created_at=at(8), last_active_at=at(8)),
         AgentModel(id="agent-resumed", team_id="team-resumed", name="b", role="leader", status="busy",
                    created_at=at(8), last_active_at=at(8)),
         EventModel(id="ev-old", type="x", source="s", timestamp=at(8)),
         EventModel(id="ev-fork", type="x", source="s", timestamp=FORK),
         *[_observation(seq, at(8, seq), "air-src") for seq in (1, 2, 3)],
         _snapshot("snap-base", at(8, 5), 3),
         CodexUsageSourceCursorModel(source_id="cursor-air", source_namespace="ns", revision=1, payload={}),
         AccountPlanSummaryModel(account_key="a" * 64, kind="plan", payload={}, updated_at=at(8)),
         GovernanceLeaseModel(id="governance", holder="api-1", expires_at="x", updated_at="x"))
    return path


def _fork(snapshot: Path, tmp_path: Path) -> tuple[Path, Path]:
    edge, center = tmp_path / "air.db", tmp_path / "mini.db"
    shutil.copyfile(snapshot, edge)
    shutil.copyfile(snapshot, center)
    return edge, center


def _diverge(edge: Path, center: Path) -> None:
    """What each machine did after the fork."""
    _sql(edge, "UPDATE tasks SET status = 'completed', completed_at = ?, result = 'Air 完成', config = ? "
               "WHERE id = 't-conflict'",
         ("2026-11-15 10:20:00.000000", json.dumps({"task_management": {"revision": 4}})))
    _sql(edge, "UPDATE tasks SET status = 'completed', result = 'Air 的结果', config = ? WHERE id = 't-done'",
         (json.dumps({"task_management": {"revision": 2}}),))
    _sql(edge, "UPDATE agents SET last_active_at = ?, status = 'busy' WHERE id = 'agent-air'",
         ("2026-11-15 10:10:00.000000",))
    _sql(edge, "UPDATE agents SET status = 'offline' WHERE id = 'agent-resumed'")  # the edge's reaper
    _sql(edge, "DELETE FROM memories WHERE id = 'mem-revived'")
    _sql(edge, "UPDATE memories SET accessed_at = ? WHERE id = 'mem-keep'", ("2026-11-15 10:00:00.000000",))
    _sql(edge, "UPDATE memories SET invalid_at = ?, invalidated_by = 'by-air' WHERE id = 'mem-twice'",
         ("2026-11-15 10:10:00.000000",))
    _sql(edge, "UPDATE meetings SET participants = ? WHERE id = 'meet'", (json.dumps(["a", "air"]),))
    _put(edge, _task("t-air-new", created_at=at(10, 5)),
         TaskMemoModel(id="memo-air", task_id="t-conflict", project_id=PROJECT, content="Air 写的",
                       created_at=at(10, 6)),
         EventModel(id="ev-air", type="x", source="s", timestamp=at(10, 10)),
         AgentActivityModel(id="act-air", agent_id="agent-air", session_id="s-a", tool_name="Bash",
                            timestamp=at(10, 10)),
         *[_observation(seq, at(10, seq), "air-src") for seq in (4, 5)],
         _snapshot("snap-air", at(10, 2), 5))

    _sql(center, "UPDATE tasks SET status = 'running', started_at = ?, title = 'mini 标题', config = ? "
                 "WHERE id = 't-conflict'", ("2026-11-15 10:15:00.000000",
                                             json.dumps({"task_management": {"revision": 5}})))
    _sql(center, "UPDATE tasks SET status = 'completed', result = 'mini 的结果', config = ? WHERE id = 't-done'",
         (json.dumps({"task_management": {"revision": 3}}),))
    _sql(center, "UPDATE agents SET status = 'offline' WHERE id = 'agent-air'")  # the center's reaper
    _sql(center, "UPDATE agents SET last_active_at = ? WHERE id = 'agent-resumed'", ("2026-11-15 10:40:00.000000",))
    _sql(center, "UPDATE teams SET summary = '在 mini 上继续' WHERE id = 'team-resumed'")
    _sql(center, "UPDATE memories SET invalid_at = ? WHERE id = 'mem-revived'", ("2026-11-15 10:50:00.000000",))
    _sql(center, "DELETE FROM memories WHERE id = 'mem-gone'")
    _sql(center, "UPDATE memories SET accessed_at = ? WHERE id = 'mem-keep'", ("2026-11-15 11:00:00.000000",))
    _sql(center, "UPDATE memories SET invalid_at = ?, invalidated_by = 'by-mini' WHERE id = 'mem-twice'",
         ("2026-11-15 10:05:00.000000",))
    _sql(center, "UPDATE meetings SET participants = ?, status = 'concluded', concluded_at = ? WHERE id = 'meet'",
         (json.dumps(["a", "mini"]), "2026-11-15 10:20:00.000000"))
    _sql(center, "DELETE FROM reports WHERE id = 'report-gone'")
    _sql(center, "UPDATE governance_lease SET holder = 'api-2'")
    _put(center, _task("t-mini-new", created_at=at(10, 7)),
         TaskMemoModel(id="memo-mini", task_id="t-mini-new", project_id=PROJECT, content="mini 写的",
                       created_at=at(10, 8)),
         TeamModel(id="team-mini", name="session-cccc", status="active", created_at=at(10, 8), updated_at=at(10, 8)),
         AgentModel(id="agent-mini", team_id="team-mini", name="c", role="leader", status="busy",
                    created_at=at(10, 8), last_active_at=at(10, 30)),
         AgentActivityModel(id="act-mini", agent_id="agent-mini", session_id="s-c", tool_name="Read",
                            timestamp=at(10, 30)),
         EventModel(id="ev-mini", type="x", source="s", timestamp=at(10, 30)),
         *[_observation(seq, at(10, seq), "mini-src") for seq in (4, 5, 6)],
         _snapshot("snap-mini", at(10, 9), 5), _snapshot("snap-mini-early", at(10, 1), 3),
         CodexUsageSourceCursorModel(source_id="cursor-mini", source_namespace="ns", revision=1, payload={}))


def _node_file(path: Path) -> Path:
    path.write_text(NodeIdentity(node_id=CENTER_NODE, role=NodeRole.CENTER, hardware_uuid="X").model_dump_json())
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(edge: Path, center: Path, out: Path, *extra: str) -> int:
    return mf.main(["--edge", str(edge), "--center", str(center), "--out-dir", str(out), *extra])


def _read(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


@pytest.fixture()
def merged(snapshot_db: Path, tmp_path: Path) -> tuple[Path, dict]:
    """A dry run of the full scenario: the merged database and its report."""
    edge, center = _fork(snapshot_db, tmp_path)
    _diverge(edge, center)
    before = (_sha(edge), _sha(center))
    out = tmp_path / "out"
    assert _run(edge, center, out, "--center-node", CENTER_NODE) == 0
    assert (_sha(edge), _sha(center)) == before, "the inputs must never be written"
    report = json.loads((out / "preview" / "merged.report.json").read_text(encoding="utf-8"))
    return out / "preview" / "merged.db", report


def test_the_fork_time_is_the_last_event_both_hold(merged) -> None:
    _, report = merged
    assert datetime.fromisoformat(report["fork_at"]) == FORK


def test_rows_of_both_sides_are_kept_and_the_centers_carry_its_node(merged) -> None:
    db, _ = merged
    origins = dict(_read(db, "SELECT id, origin_node FROM tasks WHERE project_id = ?", (PROJECT,)))
    assert origins == {"t-conflict": "", "t-done": "", "t-air-new": "", "t-mini-new": CENTER_NODE}
    assert dict(_read(db, "SELECT id, origin_node FROM events")) == {
        "ev-old": "", "ev-fork": "", "ev-air": "", "ev-mini": CENTER_NODE}
    assert dict(_read(db, "SELECT id, origin_node FROM teams WHERE id = 'team-mini'")) == {"team-mini": CENTER_NODE}
    assert dict(_read(db, "SELECT id, origin_node FROM agent_activities")) == {"act-air": "", "act-mini": CENTER_NODE}
    assert {row[0] for row in _read(db, "SELECT id FROM task_memos")} >= {"memo-base", "memo-air", "memo-mini"}


def test_a_task_both_changed_is_merged_by_the_sync_rules(merged) -> None:
    db, report = merged
    status, title, result, config = _read(
        db, "SELECT status, title, result, config FROM tasks WHERE id = 't-conflict'")[0]
    # The status further along wins with its companions; the title goes to the later writer
    # (more revisions); a row equal to neither side gets a revision past both.
    assert (status, title, result) == ("completed", "mini 标题", "Air 完成")
    assert json.loads(config)["task_management"]["revision"] == 6
    lost = {(c["field"], c["winner"], c["loser_value"], c["rule"]) for c in report["conflicts"]
            if c["table"] == "tasks" and c["pk"] == "t-conflict"}
    assert ("status", "air", "running", "status_rank") in lost
    assert ("title", CENTER_NODE, "原标题", "last_writer") in lost


def test_two_completions_keep_one_result_and_file_the_other_as_a_memo(merged) -> None:
    db, report = merged
    assert _read(db, "SELECT result FROM tasks WHERE id = 't-done'") == [("mini 的结果",)]
    memos = _read(db, "SELECT content, author, memo_type FROM task_memos WHERE task_id = 't-done'")
    assert len(memos) == 1 and "Air 的结果" in memos[0][0] and memos[0][1:] == ("sync", "issue")
    assert report["memos"][0]["loser"] == "air"


def test_a_session_row_follows_the_machine_it_last_ran_on(merged) -> None:
    db, _ = merged
    resumed = _read(db, "SELECT status, last_active_at, origin_node FROM agents WHERE id = 'agent-resumed'")
    assert resumed == [("busy", "2026-11-15 10:40:00.000000", CENTER_NODE)]
    assert _read(db, "SELECT summary, origin_node FROM teams WHERE id = 'team-resumed'") == [
        ("在 mini 上继续", CENTER_NODE)]
    # The center's reaper marking the edge's running agent offline is governance, not activity.
    assert _read(db, "SELECT status, origin_node FROM agents WHERE id = 'agent-air'") == [("busy", "")]


def test_the_centers_journal_is_renumbered_after_the_edges_and_positions_follow(merged) -> None:
    db, report = merged
    rows = _read(db, "SELECT observation_id, seq, origin_seq, origin_node FROM codex_usage_observations ORDER BY seq")
    assert [row[1] for row in rows] == list(range(1, 9))
    assert rows[5:] == [("obs-mini-src-4", 6, None, CENTER_NODE), ("obs-mini-src-5", 7, None, CENTER_NODE),
                        ("obs-mini-src-6", 8, None, CENTER_NODE)]
    positions = {sid: json.loads(payload)["activity_journal_seq"]
                 for sid, payload in _read(db, "SELECT id, payload FROM account_plan_snapshots")}
    # The center's position 5 was its own fifth fact (now 7); position 3 lay in the
    # snapshot, which is not the center's partition: it starts after the edge's facts.
    assert positions == {"snap-base": 3, "snap-air": 5, "snap-mini": 7, "snap-mini-early": 5}
    assert report["renumbering"]["codex_usage_observations"]["center_new"] == [6, 8]


def test_deletions_stay_unless_the_other_side_changed_the_row(merged) -> None:
    db, report = merged
    memories = dict(_read(db, "SELECT id, invalid_at FROM memories"))
    assert "mem-gone" not in memories and "report-gone" not in {r[0] for r in _read(db, "SELECT id FROM reports")}
    assert memories["mem-revived"] == "2026-11-15 10:50:00.000000"  # deleted on the edge, invalidated on the center
    decisions = {(d["table"], d["pk"]): (d["honoured"], d["rule"]) for d in report["deletions"]}
    assert decisions[("memories", "mem-gone")] == (True, "deleted")
    assert decisions[("memories", "mem-revived")] == (False, "delete_vs_update")


def test_derived_and_process_state_is_emptied_and_local_rows_are_joined(merged) -> None:
    db, _ = merged
    assert _read(db, "SELECT count(*) FROM account_plan_summaries") == [(0,)]
    assert _read(db, "SELECT count(*) FROM governance_lease") == [(0,)]
    cursors = {r[0] for r in _read(db, "SELECT source_id FROM codex_usage_source_cursors")}
    assert cursors == {"cursor-air", "cursor-mini"}
    assert _read(db, "SELECT count(*) FROM sync_changes") == [(0,)]
    assert _read(db, "SELECT capture, registered_node FROM sync_state") == [(0, "")]
    assert _read(db, "PRAGMA quick_check") == [("ok",)]


def test_a_different_clock_convention_or_schema_stops_the_merge(snapshot_db: Path, tmp_path: Path) -> None:
    edge, center = _fork(snapshot_db, tmp_path)
    _sql(center, "PRAGMA user_version = 1")
    assert _run(edge, center, tmp_path / "a", "--center-node", CENTER_NODE) == mf.EXIT_MISMATCH
    assert not (tmp_path / "a" / "preview" / "merged.db").exists()
    edge, center = _fork(snapshot_db, tmp_path)
    _sql(center, "ALTER TABLE tasks ADD COLUMN extra TEXT")
    assert _run(edge, center, tmp_path / "b", "--center-node", CENTER_NODE) == mf.EXIT_MISMATCH
    aborted = json.loads((tmp_path / "b" / "preview" / "aborted.report.json").read_text(encoding="utf-8"))
    assert aborted["abort"]["details"][0]["object"] == "table tasks"


def test_column_order_and_create_text_do_not_count_as_schema(tmp_path: Path) -> None:
    con = sqlite3.connect(":memory:")
    con.execute(f"ATTACH '{tmp_path / 'x.db'}' AS e")
    con.execute(f"ATTACH '{tmp_path / 'y.db'}' AS c")
    con.execute("CREATE TABLE e.t (a VARCHAR(5), b INTEGER)")
    con.execute("CREATE TABLE c.t (b INT NOT NULL DEFAULT 0)")
    con.execute("ALTER TABLE c.t ADD COLUMN a TEXT")
    assert mf.schema_signature(con, "e") == mf.schema_signature(con, "c")
    con.execute("ALTER TABLE c.t ADD COLUMN z REAL")
    assert mf.schema_signature(con, "e") != mf.schema_signature(con, "c")


def test_a_column_declared_boolean_then_integer_is_one_schema(tmp_path: Path) -> None:
    """sync_state.capture: Boolean (NUMERIC) on a database upgraded through phase 0, Integer
    on one created later; both hold the same integers. A real type change still stops it."""
    con = sqlite3.connect(":memory:")
    con.execute(f"ATTACH '{tmp_path / 'x.db'}' AS e")
    con.execute(f"ATTACH '{tmp_path / 'y.db'}' AS c")
    con.execute("CREATE TABLE e.t (capture BOOLEAN NOT NULL)")
    con.execute("CREATE TABLE c.t (capture INTEGER NOT NULL)")
    assert mf.schema_signature(con, "e") == mf.schema_signature(con, "c")
    con.execute("CREATE TABLE e.u (v TEXT)")
    con.execute("CREATE TABLE c.u (v INTEGER)")
    assert mf.schema_signature(con, "e") != mf.schema_signature(con, "c")


def test_a_database_that_already_syncs_is_refused(snapshot_db: Path, tmp_path: Path) -> None:
    edge, center = _fork(snapshot_db, tmp_path)
    _sql(center, "UPDATE sync_state SET registered_node = 'mini-0000'")
    assert _run(edge, center, tmp_path / "out", "--center-node", CENTER_NODE) == mf.EXIT_SYNCED


def test_one_unique_value_held_by_two_new_rows_stops_the_merge(snapshot_db: Path, tmp_path: Path) -> None:
    edge, center = _fork(snapshot_db, tmp_path)
    _put(edge, TeamModel(id="team-x-air", name="session-dddd", created_at=at(10), updated_at=at(10)))
    _put(center, TeamModel(id="team-x-mini", name="session-dddd", created_at=at(10), updated_at=at(10)))
    out = tmp_path / "out"
    assert _run(edge, center, out, "--center-node", CENTER_NODE) == mf.EXIT_COLLISION
    aborted = json.loads((out / "preview" / "aborted.report.json").read_text(encoding="utf-8"))
    assert aborted["collisions"] == [{"table": "teams", "columns": ["name"], "value": ["session-dddd"],
                                      "center_row": "team-x-mini", "edge_row": "team-x-air"}]
    assert not (out / "preview" / "merged.db").exists()


def test_apply_archives_read_only_and_a_second_run_finds_the_result(snapshot_db: Path, tmp_path: Path,
                                                                    capsys) -> None:
    edge, center = _fork(snapshot_db, tmp_path)
    _diverge(edge, center)
    out = tmp_path / "out"
    assert _run(edge, center, out, "--center-node", CENTER_NODE, "--apply") == mf.EXIT_USAGE  # needs node.json
    node = _node_file(tmp_path / "node.json")
    assert _run(edge, center, out, "--center-node-file", str(node), "--apply") == 0
    manifest = json.loads((out / "archive" / "MANIFEST.json").read_text(encoding="utf-8"))
    for entry, source in zip(manifest["files"], (edge, center), strict=True):
        archived = out / "archive" / entry["archive"]
        assert entry["sha256"] == _sha(archived) == _sha(source)
        assert not archived.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    (merged,) = out.glob("merged-*.db")
    assert _read(merged, "SELECT origin_node FROM tasks WHERE id = 't-mini-new'") == [(CENTER_NODE,)]
    stamp = merged.stat().st_mtime_ns
    capsys.readouterr()
    assert _run(edge, center, out, "--center-node-file", str(node), "--apply") == 0
    assert "已合并过" in capsys.readouterr().out and merged.stat().st_mtime_ns == stamp
    assert not list(out.glob(".work-*"))


def test_a_run_that_dies_leaves_no_result_and_the_next_one_cleans_up(snapshot_db: Path, tmp_path: Path,
                                                                     monkeypatch) -> None:
    edge, center = _fork(snapshot_db, tmp_path)
    out = tmp_path / "out"

    def die(self) -> None:
        raise RuntimeError("killed")

    with monkeypatch.context() as patch:
        patch.setattr(mf.Merger, "_reset_sync_bookkeeping", die)
        with pytest.raises(RuntimeError):
            _run(edge, center, out, "--center-node", CENTER_NODE)
    assert not (out / "preview").exists() and not list(out.glob(".work-*"))
    (out / ".work-99999").mkdir()  # what a hard kill leaves behind
    (out / ".work-99999" / "merged.db").write_bytes(b"half")
    assert _run(edge, center, out, "--center-node", CENTER_NODE) == 0
    assert not list(out.glob(".work-*")) and (out / "preview" / "merged.db").exists()


def test_fields_that_only_advance_join_and_the_first_invalidation_stays(merged) -> None:
    db, report = merged
    assert _read(db, "SELECT accessed_at FROM memories WHERE id = 'mem-keep'") == [("2026-11-15 11:00:00.000000",)]
    assert _read(db, "SELECT invalid_at, invalidated_by FROM memories WHERE id = 'mem-twice'") == [
        ("2026-11-15 10:05:00.000000", "by-mini")]
    participants, status = _read(db, "SELECT participants, status FROM meetings WHERE id = 'meet'")[0]
    assert (json.loads(participants), status) == (["a", "air", "mini"], "concluded")
    rules = {(c["table"], c["pk"], c["field"]): c["rule"] for c in report["conflicts"]}
    assert rules[("memories", "mem-twice", "invalid_at")] == "invalidated_first"
    assert ("memories", "mem-keep", "accessed_at") not in rules and ("meetings", "meet", "participants") not in rules


def test_a_project_deletion_takes_its_rows_unless_the_other_side_kept_using_it(snapshot_db: Path,
                                                                                tmp_path: Path) -> None:
    edge, center = _fork(snapshot_db, tmp_path)
    for project in (GONE, KEPT):  # the center deleted both projects with their rows
        for table, column in (("tasks", "project_id"), ("reports", "project_id"), ("projects", "id")):
            _sql(center, f"DELETE FROM {table} WHERE {column} = ?", (project,))
    _put(edge, _task("task-new-in-kept", project=KEPT, created_at=at(10)))  # the edge kept working in one
    out = tmp_path / "out"
    assert _run(edge, center, out, "--center-node", CENTER_NODE) == 0
    db = out / "preview" / "merged.db"
    assert _read(db, "SELECT id FROM projects WHERE id IN (?, ?)", (GONE, KEPT)) == [(KEPT,)]
    assert {r[0] for r in _read(db, "SELECT id FROM tasks WHERE project_id IN (?, ?)", (GONE, KEPT))} == {
        "task-2", "task-new-in-kept"}
    assert _read(db, "SELECT id FROM reports WHERE project_id IN (?, ?)", (GONE, KEPT)) == [("report-2",)]
    report = json.loads((out / "preview" / "merged.report.json").read_text(encoding="utf-8"))
    rules = {(d["table"], d["pk"]): d["rule"] for d in report["deletions"]}
    assert rules[("projects", GONE)] == "deleted" and rules[("tasks", "task-1")] == "project_deleted"
    assert rules[("projects", KEPT)] == "delete_vs_update" and rules[("reports", "report-2")] == "project_kept"


def test_the_rule_tables_name_real_tables_and_columns() -> None:
    from aiteam.storage.models import Base

    columns = {name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()}
    assert mf.DELETABLE <= SHARED_TABLES and set(mf.CLEARED) <= set(LOCAL_TABLES)
    assert set(mf.STATUS_GROUPS) <= set(STATUS_RANKS)
    assert all(mf.BORN[table] in columns[table] for table in mf.DELETABLE)
    assert all("project_id" in columns[t] and "created_at" in columns[t] for t in mf.PROJECT_CHILDREN)
    for rules in (mf.WATERMARKS, mf.STATUS_GROUPS, mf.UNION_LISTS):
        assert all(set(fields) <= columns[table] for table, fields in rules.items())
    assert all(set(mf._INVALIDATION) <= columns[table] for table in mf.INVALIDATION_TABLES)


def test_a_second_run_on_the_same_output_waits_its_turn(snapshot_db: Path, tmp_path: Path) -> None:
    import fcntl

    edge, center = _fork(snapshot_db, tmp_path)
    out = tmp_path / "out"
    assert _run(edge, center, out, "--center-node", CENTER_NODE) == 0
    with (out / ".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _run(edge, center, out, "--center-node", CENTER_NODE) == mf.EXIT_USAGE
    # The running one's files are left alone.
    assert (out / "preview" / "merged.db").exists() and not (out / "preview" / "aborted.report.json").exists()


def test_the_application_reads_the_merged_database(merged) -> None:
    db, _ = merged

    async def read() -> tuple:
        repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db}")
        await repo.init_db()  # a later start migrates nothing and keeps every row
        try:
            return await repo.get_task("t-conflict"), await repo.list_task_memos("t-done")
        finally:
            await close_db()

    task, memos = asyncio.run(read())
    assert (task.status, task.title, task.config["task_management"]["revision"]) == ("completed", "mini 标题", 6)
    assert [memo.memo_type for memo in memos] == ["issue"] and memos[0].meta == {}
