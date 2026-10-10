"""Real SQLite boundaries for read-only preview, guarded apply and rerun."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/backfill_codex_usage.py"
spec = importlib.util.spec_from_file_location("codex_backfill", SCRIPT)
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
        con.execute("pragma user_version=20260727")
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


async def _codex_row(db, sid, path, **fields):
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db}")
    actor = await repo.create_agent(team_id="t", name="Codex", role="leader", session_id=sid)
    await repo.update_agent(actor.id, harness="codex", transcript_path=str(path), **fields)
    return actor.id


def _meta_only(home, sid):
    folder = home / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-test-{sid}.jsonl"
    meta = dict(timestamp="2026-09-30T00:00:00Z", type="session_meta", payload=dict(id=sid, source="cli"))
    path.write_text(json.dumps(meta) + "\n")
    return path


def _cells(db, aid):
    with sqlite3.connect(db) as con:
        return con.execute(
            "select tokens_measured_at is not null, input_tokens, usage_unmeasurable from agents where id=?", (aid,)
        ).fetchone()


ENDED = dict(status="offline", last_active_at=backfill.parse_utc("2026-09-29T00:00:00Z"))


async def test_finished_row_without_usage_event_gets_a_journaled_verdict(sample, tmp_path):
    db, home, aid = sample
    empty = await _codex_row(db, "e", _meta_only(home, "e"), **ENDED)
    plan = backfill.create_plan(db, home)
    assert plan["counts"] == {"planned": 1, "no_usage": 1, "verdict_planned": 1, "verdict_in_flight": 0}
    assert [v["id"] for v in plan["verdicts"]] == [empty]
    assert _cells(db, empty) == (0, None, None)  # the preview writes nothing
    backfill.apply_plan(db, plan, backup=tmp_path / "backup.sqlite", journal=tmp_path / "journal.json")
    assert _cells(db, empty) == (0, None, "no_usage_lines")
    assert _cells(db, aid) == (1, 40, None)
    journal = json.loads((tmp_path / "journal.json").read_text())
    assert (journal["status"], journal["updated"], journal["marked"]) == ("applied", 1, 1)
    verdict = journal["plan"]["verdicts"][0]
    assert verdict["before"]["usage_unmeasurable"] is None
    assert verdict["after"] == {"usage_unmeasurable": "no_usage_lines"}
    rerun = backfill.create_plan(db, home)
    assert rerun["counts"] == {"already_measured": 1, "duplicate_actor": 0, "no_usage": 1}
    assert (rerun["changes"], rerun["verdicts"]) == ([], [])


@pytest.mark.parametrize(
    ("status", "idle_minutes"), [("busy", 120), ("waiting", 5)], ids=["busy", "recently-active"]
)
async def test_running_row_gets_no_verdict(sample, status, idle_minutes):
    db, home, _aid = sample
    last_active_at = backfill.utc_now() - timedelta(minutes=idle_minutes)
    running = await _codex_row(db, "e", _meta_only(home, "e"), status=status, last_active_at=last_active_at)
    plan = backfill.create_plan(db, home)
    assert plan["counts"]["verdict_in_flight"] == 1
    assert plan["verdicts"] == []
    assert _cells(db, running) == (0, None, None)


async def test_missing_rollout_is_no_verdict(sample):
    db, home, _aid = sample
    await _codex_row(db, "m", home / "sessions/rollout-test-m.jsonl", **ENDED)
    plan = backfill.create_plan(db, home)
    assert plan["counts"]["rollout_not_found"] == 1
    assert "no_usage" not in plan["counts"]
    assert plan["verdicts"] == []


async def test_row_active_again_after_preview_rejects_the_whole_apply(sample, tmp_path):
    db, home, aid = sample
    empty = await _codex_row(db, "e", _meta_only(home, "e"), **ENDED)
    plan = backfill.create_plan(db, home)
    with sqlite3.connect(db) as con:
        con.execute("update agents set status='busy' where id=?", (empty,))
    with pytest.raises(backfill.PlanConflictError, match="已变化"):
        backfill.apply_plan(db, plan, backup=tmp_path / "backup.sqlite", journal=tmp_path / "journal.json")
    assert _cells(db, empty) == (0, None, None)
    assert _cells(db, aid) == (0, None, None)
    assert json.loads((tmp_path / "journal.json").read_text())["status"] == "rejected"


async def test_measurement_clears_an_earlier_no_usage_verdict(sample, tmp_path):
    db, home, aid = sample
    with sqlite3.connect(db) as con:
        con.execute("update agents set usage_unmeasurable='no_usage_lines' where id=?", (aid,))
    plan = backfill.create_plan(db, home)
    assert plan["changes"][0]["before"]["usage_unmeasurable"] == "no_usage_lines"
    backfill.apply_plan(db, plan, backup=tmp_path / "backup.sqlite", journal=tmp_path / "journal.json")
    assert _cells(db, aid) == (1, 40, None)


async def test_edited_plans_cannot_carry_verdicts_the_rule_refuses(sample, tmp_path):
    db, home, _aid = sample
    await _codex_row(db, "e", _meta_only(home, "e"), **ENDED)
    for edit in (
        lambda p: p["verdicts"][0]["before"].update(status="busy"),
        lambda p: p["verdicts"][0]["before"].update(tokens_measured_at="2026-09-30 00:00:00.000000"),
        lambda p: p["verdicts"][0].update(after={"usage_unmeasurable": "api_error_only"}),
        lambda p: p.update(format=1),
        lambda p: p.pop("verdicts"),
    ):
        plan = backfill.create_plan(db, home)
        edit(plan)
        with pytest.raises(backfill.PlanConflictError):
            backfill.apply_plan(db, plan, backup=tmp_path / "b.sqlite", journal=tmp_path / "j.json")
        assert not (tmp_path / "b.sqlite").exists()


# ---------------------------------------------------------------------------
# Clock guard: user_version 0 or 20260728 (docs/utc-unification-design.md §6.7)
# ---------------------------------------------------------------------------
def _set_marker(db, value):
    with sqlite3.connect(db) as con:
        con.execute(f"pragma user_version={value}")


def _read_marker(db):
    with sqlite3.connect(db) as con:
        return con.execute("pragma user_version").fetchone()[0]


@pytest.mark.parametrize("marker", [0, 20260728], ids=["unmarked-0", "utc-20260728"])
async def test_unmarked_and_utc_marked_databases_plan_and_apply_alike(sample, tmp_path, marker):
    """Only harness=codex rows are written and every version that writes them writes UTC,
    so the whole-database marker does not decide their clock. A 0 is recorded as 0."""
    db, home, aid = sample
    _set_marker(db, marker)
    plan = backfill.create_plan(db, home)
    assert plan["counts"] == {"planned": 1}
    assert (plan["user_version"], plan["clock_basis"]) == (marker, backfill.CLOCK_BASIS[marker])
    journal = tmp_path / "journal.json"
    backfill.apply_plan(db, plan, backup=tmp_path / "backup.sqlite", journal=journal)
    assert _cells(db, aid) == (1, 40, None)
    record = json.loads(journal.read_text())
    assert (record["status"], record["user_version"], record["clock_basis"]) == (
        "applied", marker, backfill.CLOCK_BASIS[marker]
    )
    assert record["plan"]["user_version"] == marker
    assert _read_marker(db) == marker  # the backfill never declares a clock for the database
    assert _read_marker(tmp_path / "backup.sqlite") == marker


async def test_the_marker_changes_only_the_recorded_clock_fields_of_a_plan(sample, tmp_path):
    db, home, _aid = sample
    await _codex_row(db, "e", _meta_only(home, "e"), **ENDED)

    def comparable(plan):
        changes = [
            {**change, "after": {**change["after"], "tokens_measured_at": None}} for change in plan["changes"]
        ]
        return plan["counts"], changes, plan["verdicts"], plan["allowed_columns"]

    marked = backfill.create_plan(db, home)
    _set_marker(db, 0)
    unmarked = backfill.create_plan(db, home)
    assert comparable(unmarked) == comparable(marked)
    assert (marked["user_version"], unmarked["user_version"]) == (20260728, 0)
    assert marked["clock_basis"] != unmarked["clock_basis"]


@pytest.mark.parametrize(
    "marker", [20260727, 20260729, 1, -1], ids=["day-before", "day-after", "one", "negative"]
)
async def test_other_clock_markers_are_refused_at_preview_and_at_apply(sample, tmp_path, marker):
    db, home, aid = sample
    plan = backfill.create_plan(db, home)  # made while the database said 20260728
    _set_marker(db, marker)
    with pytest.raises(backfill.PlanConflictError, match="未知时钟"):
        backfill.create_plan(db, home)
    with pytest.raises(backfill.PlanConflictError, match="时钟"):
        backfill.apply_plan(db, plan, backup=tmp_path / "b.sqlite", journal=tmp_path / "j.json")
    assert not (tmp_path / "b.sqlite").exists()
    assert not (tmp_path / "j.json").exists()
    assert _cells(db, aid) == (0, None, None)


async def test_edited_or_stale_clock_fields_in_a_plan_are_refused(sample, tmp_path):
    db, home, aid = sample
    other = {0: 20260728, 20260728: 0}
    for edit in (
        lambda p: p.update(user_version=20260727),
        lambda p: p.update(clock_basis=backfill.CLOCK_BASIS[other[p["user_version"]]]),
        lambda p: p.pop("clock_basis"),
        lambda p: p.update(user_version=other[p["user_version"]]),  # the basis no longer matches
    ):
        plan = backfill.create_plan(db, home)
        edit(plan)
        with pytest.raises(backfill.PlanConflictError):
            backfill.apply_plan(db, plan, backup=tmp_path / "b.sqlite", journal=tmp_path / "j.json")
        assert not (tmp_path / "b.sqlite").exists()
    # A plan made at 0 is stale once the database carries 20260728 (a migration ran in between).
    _set_marker(db, 0)
    plan = backfill.create_plan(db, home)
    _set_marker(db, 20260728)
    with pytest.raises(backfill.PlanConflictError, match="时钟"):
        backfill.apply_plan(db, plan, backup=tmp_path / "b.sqlite", journal=tmp_path / "j.json")
    assert not (tmp_path / "b.sqlite").exists()
    assert _cells(db, aid) == (0, None, None)


async def test_marker_changed_after_the_backup_rejects_the_whole_transaction(sample, tmp_path, monkeypatch):
    """The check inside BEGIN IMMEDIATE is the one that holds: the marker may change between
    the backup and the write lock (a timestamp migration run in between, say)."""
    db, home, aid = sample
    _set_marker(db, 0)
    plan = backfill.create_plan(db, home)
    take_backup = backfill._take_backup

    def backup_then_migrate(*args, **kwargs):
        marker = take_backup(*args, **kwargs)
        _set_marker(db, 20260728)
        return marker

    monkeypatch.setattr(backfill, "_take_backup", backup_then_migrate)
    journal = tmp_path / "j.json"
    with pytest.raises(backfill.PlanConflictError, match="时钟标记已变"):
        backfill.apply_plan(db, plan, backup=tmp_path / "b.sqlite", journal=journal)
    assert _cells(db, aid) == (0, None, None)
    record = json.loads(journal.read_text())
    assert (record["status"], record["user_version"], record["updated"]) == ("rejected", 0, 0)
    assert _read_marker(tmp_path / "b.sqlite") == 0
    assert _read_marker(db) == 20260728  # the backfill never writes the marker back


async def test_cli_reports_a_refused_plan_in_one_line_and_exits_nonzero(sample, tmp_path):
    """The user-facing entry: a refusal is a readable line on stderr and exit code 3, not a traceback."""
    db, home, aid = sample
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AITEAM_DB_PATH": str(tmp_path / "unused.sqlite"),
        "CODEX_HOME": str(home),
    }

    def cli(*argv):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db), *argv],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )

    _set_marker(db, 0)
    plan = tmp_path / "plan.json"
    made = cli("--plan-out", str(plan))
    assert made.returncode == 0, made.stderr
    _set_marker(db, 20260728)
    backup, journal = tmp_path / "b.sqlite", tmp_path / "j.json"
    applied = cli("--apply", "--plan", str(plan), "--backup", str(backup), "--journal", str(journal))
    assert applied.returncode == 3, applied.stderr
    assert "Traceback" not in applied.stderr
    assert "时钟标记已变" in applied.stderr and "库未改动" in applied.stderr
    assert applied.stdout == ""
    assert not backup.exists() and not journal.exists()
    assert _cells(db, aid) == (0, None, None)

    _set_marker(db, 7)
    preview = cli()
    assert preview.returncode == 3, preview.stderr
    assert "Traceback" not in preview.stderr and "未知时钟" in preview.stderr
    assert preview.stdout == ""
