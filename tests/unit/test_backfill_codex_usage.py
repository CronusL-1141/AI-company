"""Real SQLite boundaries for read-only preview, guarded apply and rerun."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

spec = importlib.util.spec_from_file_location(
    "codex_backfill", Path(__file__).resolve().parents[2] / "scripts/backfill_codex_usage.py"
)
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


@pytest_asyncio.fixture
async def sample(tmp_path):
    db = tmp_path / "agents.sqlite"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db}")
    await repo.init_db()
    root = tmp_path / "codex"
    folder = root / "archived_sessions"
    folder.mkdir(parents=True)
    path = folder / "rollout-test-s.jsonl"
    path.write_text(
        json.dumps(dict(timestamp="2026-09-30T00:00:00Z", type="session_meta", payload=dict(id="s", source="cli")))
        + "\n"
        + json.dumps(
            dict(
                timestamp="2026-09-30T00:00:01Z",
                type="token_usage_record",
                payload=dict(
                    response_id="r",
                    thread_id="s",
                    usage=dict(
                        input_tokens=100,
                        cached_input_tokens=60,
                        cache_write_input_tokens=0,
                        output_tokens=20,
                        reasoning_output_tokens=5,
                        total_tokens=120,
                    ),
                ),
            )
        )
        + "\n"
    )
    actor = await repo.create_agent(team_id="t", name="Codex", role="leader", session_id="s")
    await repo.update_agent(actor.id, harness="codex", transcript_path=str(root / "sessions/gone.jsonl"))
    with sqlite3.connect(db) as con:
        con.execute("pragma user_version=20260728")
    yield db, root, actor.id
    await close_db()


async def test_readonly_preview_apply_cross_connection_and_idempotency(sample, tmp_path):
    db, home, aid = sample
    con = backfill.open_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("update agents set input_tokens=999")
    con.close()
    plan = backfill.create_plan(db, home)
    assert plan["counts"] == {"planned": 1}
    with sqlite3.connect(db) as con:
        assert con.execute("select tokens_measured_at from agents where id=?", (aid,)).fetchone()[0] is None
    backfill.apply_plan(db, plan, backup=tmp_path / "backup.sqlite", journal=tmp_path / "journal.json")
    with sqlite3.connect(db) as con:
        row = con.execute(
            "select input_tokens,output_tokens,cache_read_tokens,reasoning_output_tokens,tokens_source,"
            "transcript_path from agents where id=?",
            (aid,),
        ).fetchone()
    assert row[:5] == (40, 20, 60, 5, "codex_rollout")
    assert "archived_sessions" in row[5]
    journal = json.loads((tmp_path / "journal.json").read_text())
    assert journal["plan"]["changes"][0]["before"]["transcript_path"] == str(home / "sessions/gone.jsonl")
    assert journal["plan"]["changes"][0]["after"]["transcript_path"] == row[5]
    assert backfill.create_plan(db, home)["counts"] == {"already_measured": 1, "duplicate_actor": 0}


async def test_concurrent_change_rejects_whole_transaction(sample, tmp_path):
    db, home, aid = sample
    plan = backfill.create_plan(db, home)
    with sqlite3.connect(db) as con:
        con.execute(
            "update agents set tokens_measured_at=?, input_tokens=700 where id=?", ("2026-09-30 00:00:05.000000", aid)
        )
    with pytest.raises(backfill.PlanConflictError, match="已变化"):
        backfill.apply_plan(db, plan, backup=tmp_path / "backup.sqlite", journal=tmp_path / "journal.json")
    with sqlite3.connect(db) as con:
        assert con.execute("select input_tokens from agents where id=?", (aid,)).fetchone()[0] == 700
    assert json.loads((tmp_path / "journal.json").read_text())["status"] == "rejected"


async def test_unknown_clock_invalid_plan_and_duplicate_actor_are_not_measured(sample, tmp_path):
    db, home, aid = sample
    plan = backfill.create_plan(db, home)
    plan["changes"][0]["after"]["tokens_measured_at"] = "garbage"
    with pytest.raises(backfill.PlanConflictError, match="naive UTC"):
        backfill.apply_plan(db, plan, backup=tmp_path / "backup.sqlite", journal=tmp_path / "journal.json")
    with sqlite3.connect(db) as con:
        con.execute("pragma user_version=0")
    with pytest.raises(backfill.PlanConflictError, match="未知时钟"):
        backfill.create_plan(db, home)


async def test_duplicate_actor_without_measurement_is_not_arbitrarily_chosen(sample):
    db, home, aid = sample
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db}")
    duplicate = await repo.create_agent(team_id="other", name="Copy", role="leader", session_id="s")
    await repo.update_agent(duplicate.id, harness="codex", transcript_path=str(home / "sessions/gone.jsonl"))
    plan = backfill.create_plan(db, home)
    assert plan["counts"] == {"duplicate_actor": 2}
    assert plan["changes"] == []
    with sqlite3.connect(db) as con:
        assert con.execute("select count(*) from agents where tokens_measured_at is not null").fetchone()[0] == 0
    await repo.update_agent(
        aid,
        tokens_measured_at=backfill.parse_utc("2026-09-30T00:00:05Z"),
        input_tokens=40,
        tokens_source="codex_rollout",
    )
    rerun = backfill.create_plan(db, home)
    assert rerun["counts"] == {"already_measured": 1, "duplicate_actor": 1}
    assert rerun["changes"] == []


async def test_cli_default_home_follows_environment_and_canonical_root(sample, tmp_path, monkeypatch, capsys):
    db, home, aid = sample
    linked = tmp_path / "linked-home"
    linked.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(linked))
    monkeypatch.setattr(backfill.sys, "argv", ["backfill_codex_usage.py", "--db", str(db)])
    backfill.main()
    assert json.loads(capsys.readouterr().out)["counts"] == {"planned": 1}
