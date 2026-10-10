"""``aiteam attribution repair``: dry run, guarded apply, rerun and undo on real SQLite files.

Databases are built from the production DDL of the three tables the repair reads
(tests/fixtures/attribution_repair/schema.sql); transcripts are real files under a
temporary ``CLAUDE_CONFIG_DIR``. Every assertion reads the database again through a new
connection, and the CLI runs through the top-level ``aiteam`` app.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiteam.api.session_probe import SYNTHETIC_MODEL
from aiteam.cli.app import app
from aiteam.services import attribution_repair
from aiteam.services.token_attribution import unmeasurable_reason
from aiteam.services.usage_coverage import is_in_flight

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SCHEMA = FIXTURES / "attribution_repair" / "schema.sql"
TARGETS = attribution_repair.TARGET_COLUMNS
# A real Codex rollout (16 token_count events, no Claude Code assistant line).
CODEX_THREAD = "019f93ea-9368-7241-a382-bee12ae67c96"
CODEX_ROLLOUT = (FIXTURES / "codex" / "rollouts" / "native" / "sessions" / "2026" / "07" / "24"
                 / f"rollout-2026-07-24T19-37-31-{CODEX_THREAD}.jsonl")

SHELL_SID = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
YOUNG_SID = "7a2b3c4d-5e6f-4a7b-9c8d-1e2f3a4b5c6d"
ACTIVE_SID = "8b3c4d5e-6f7a-4b8c-ad9e-2f3a4b5c6d7e"
ALREADY_SID = "9c4d5e6f-7a8b-4c9d-be0f-3a4b5c6d7e8f"
DUP_SID = "abff40af-58a1-4a7e-84ee-a68f07f72ed3"
THREAD_A = "0199a1b2-c3d4-7e5f-8a6b-1c2d3e4f5a6b"  # UUIDv7 shapes (Codex thread ids)
THREAD_B = "0199a1b2-c3d4-7e5f-8a6b-1c2d3e4f5a6c"

USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
BILLED = {"type": "assistant", "message": {"model": "claude-test", "usage": USAGE}}
SYNTHETIC = {"type": "assistant", "message": {"model": SYNTHETIC_MODEL, "usage": USAGE}}
PROMPT = {"type": "user", "message": {"role": "user", "content": "start"}}


def ts(value: datetime) -> str:
    """Storage format: naive UTC with microseconds."""
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


class World:
    def __init__(self, root: Path, *, schema: str | None = None) -> None:
        self.root = root
        self.claude = root / ".claude"
        self.projects = self.claude / "projects"
        self.codex = root / ".codex"
        self.db = root / "data" / "aiteam.db"
        self.out = root / "out"
        self.now = datetime.now(UTC)
        self.db.parent.mkdir(parents=True)
        self.projects.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript(schema if schema is not None else SCHEMA.read_text(encoding="utf-8"))
            con.execute(f"PRAGMA user_version = {attribution_repair.UTC_CLOCK_MARKER}")
            con.commit()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with closing(sqlite3.connect(self.db)) as con:
            con.execute(sql, params)
            con.commit()

    def team(self, team_id: str, name: str) -> None:
        now = ts(self.now)
        self.execute(
            "INSERT INTO teams (id, name, mode, status, summary, config, created_at, updated_at) "
            "VALUES (?, ?, 'coordinate', 'active', '', '{}', ?, ?)",
            (team_id, name, now, now),
        )

    def agent(self, agent_id: str, **columns: object) -> None:
        row: dict[str, object] = {
            "id": agent_id, "team_id": "team-s", "name": agent_id, "role": "general", "system_prompt": "",
            "model": "", "status": "offline", "config": "{}", "source": "hook", "trust_score": 0.5,
            "created_at": ts(self.now - timedelta(days=2)),
        }
        row.update(columns)
        names = ", ".join(row)
        marks = ", ".join("?" * len(row))
        self.execute(f"INSERT INTO agents ({names}) VALUES ({marks})", tuple(row.values()))  # noqa: S608

    def activity(self, agent_id: str) -> None:
        self.execute(
            "INSERT INTO agent_activities (id, agent_id, session_id, tool_name, input_summary, output_summary, "
            "timestamp, status) VALUES (?, ?, 's', 'Bash', '', '', ?, 'completed')",
            (f"act-{agent_id}", agent_id, ts(self.now - timedelta(days=2))),
        )

    def transcript(self, relative: str, *lines: dict) -> str:
        path = self.projects / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        return str(path)

    def dump(self, db: Path | None = None) -> dict[str, dict]:
        with closing(sqlite3.connect(db or self.db)) as con:
            con.row_factory = sqlite3.Row
            return {row["id"]: dict(row) for row in con.execute("SELECT * FROM agents")}

    def user_version(self, value: int) -> None:
        self.execute(f"PRAGMA user_version = {value}")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = World(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(built.claude))
    monkeypatch.setenv("CODEX_HOME", str(built.codex))
    monkeypatch.setenv("AITEAM_DB_PATH", str(built.db))
    return built


def run(*args: str):
    return CliRunner().invoke(app, ["attribution", "repair", *args])


def populate(world: World) -> dict[str, dict[str, str]]:
    """One group per rule, plus the rows each rule must leave alone. Returns the cells
    the repair must write, keyed by agent id."""
    now = world.now
    world.team("team-s", "session-6f1c2d3e")
    world.team("team-wf", "workflow-wf_20261010")
    shared = world.transcript(f"-p/{DUP_SID}.jsonl", PROMPT, BILLED)
    # Resume shells: a session id, no transcript, no tool call, offline past the grace period.
    world.agent("shell", role="leader", session_id=SHELL_SID)
    world.agent("shell-young", role="leader", session_id=YOUNG_SID, created_at=ts(now - timedelta(minutes=10)))
    world.agent("shell-active", role="leader", session_id=ACTIVE_SID)
    world.activity("shell-active")
    world.agent("already", role="leader", session_id=ALREADY_SID, harness="claude-code",
                attribution_exclusion="resume_shell")
    # Three Leader rows on one transcript: the measured one with a session represents.
    world.agent("dup-rep", role="leader", harness="claude-code", session_id=DUP_SID, transcript_path=shared,
                tokens_measured_at=ts(now - timedelta(hours=1)), created_at=ts(now - timedelta(days=3)),
                last_active_at=ts(now - timedelta(hours=1)))
    world.agent("dup-a", role="leader", transcript_path=shared, created_at=ts(now - timedelta(days=5)))
    world.agent("dup-b", role="leader", transcript_path=shared, created_at=ts(now - timedelta(days=4)))
    # Unlabelled harness with and without evidence.
    world.agent("wf-sub", role="workflow-subagent", team_id="team-wf")
    world.agent("codex-thread", session_id=THREAD_A, cc_tool_use_id=THREAD_B)
    world.agent("unknown", source="api")
    # Finished and running rows whose transcript has no billed call.
    finished = {"harness": "claude-code", "created_at": ts(now - timedelta(hours=3)),
                "last_active_at": ts(now - timedelta(hours=2))}
    world.agent("done-nousage", transcript_path=world.transcript(f"-p/{DUP_SID}/subagents/agent-a1.jsonl", PROMPT),
                **finished)
    world.agent("done-apierror",
                transcript_path=world.transcript(f"-p/{DUP_SID}/subagents/agent-a2.jsonl", PROMPT, SYNTHETIC),
                **finished)
    world.agent("busy-nousage", transcript_path=world.transcript(f"-p/{DUP_SID}/subagents/agent-a3.jsonl", PROMPT),
                **{**finished, "status": "busy"})
    world.agent("recent-nousage",
                transcript_path=world.transcript(f"-p/{DUP_SID}/subagents/agent-a4.jsonl", PROMPT),
                **{**finished, "last_active_at": ts(now - timedelta(minutes=5))})
    return {
        "shell": {"harness": "claude-code", "attribution_exclusion": "resume_shell"},
        "shell-young": {"harness": "claude-code"},
        "shell-active": {"harness": "claude-code"},
        "dup-a": {"harness": "claude-code", "attribution_exclusion": "duplicate_transcript",
                  "attribution_exclusion_of": "dup-rep"},
        "dup-b": {"harness": "claude-code", "attribution_exclusion": "duplicate_transcript",
                  "attribution_exclusion_of": "dup-rep"},
        "wf-sub": {"harness": "claude-code"},
        "codex-thread": {"harness": "codex"},
        "done-nousage": {"usage_unmeasurable": "no_usage_lines"},
        "done-apierror": {"usage_unmeasurable": "api_error_only"},
    }


def assert_written(after: dict, original: dict, expected: dict) -> None:
    assert set(after) == set(original), "rows appeared or vanished"
    for agent_id, row in after.items():
        for column, value in row.items():
            want = expected.get(agent_id, {}).get(column, original[agent_id][column])
            assert value == want, f"{agent_id}.{column}: {value!r} != {want!r}"


def only(directory: Path, pattern: str) -> Path:
    found = sorted(directory.glob(pattern))
    assert len(found) == 1, found
    return found[0]


def test_dry_run_apply_rerun_and_undo_round_trip(world: World) -> None:
    expected = populate(world)
    original = world.dump()

    preview = run()  # default database: AITEAM_DB_PATH, as the API resolves it
    assert preview.exit_code == 0, preview.output
    assert "计划写入 14 格（9 行）" in preview.output
    for line in ("harness=claude-code: 6", "harness=codex: 1", "attribution_exclusion=resume_shell: 1",
                 "attribution_exclusion=duplicate_transcript: 2", "attribution_exclusion_of: 2",
                 "usage_unmeasurable=no_usage_lines: 1", "usage_unmeasurable=api_error_only: 1",
                 "在飞、本次不下结论的未测量行：2", "认不出来源、保持未标注 harness 的行：1"):
        assert line in preview.output, line
    assert world.dump() == original
    assert not (world.db.parent / "attribution-repair").exists()

    applied = run("--apply", "--out-dir", str(world.out))
    assert applied.exit_code == 0, applied.output
    after = world.dump()
    assert_written(after, original, expected)
    journal = only(world.out, "*.journal.json")
    record = json.loads(journal.read_text(encoding="utf-8"))
    assert record["status"] == "applied"
    assert record["cells"] == 14
    assert record["user_version"] == attribution_repair.UTC_CLOCK_MARKER
    written = {(c["agent_id"], cell["column"], cell["new"]) for c in record["changes"] for cell in c["cells"]}
    assert written == {(aid, col, val) for aid, cells in expected.items() for col, val in cells.items()}
    assert set(record["before"]) == set(expected) | {"dup-rep"}, "representatives belong to the before-image"
    backup = only(world.out, "aiteam.db.bak-attribution-*")
    assert world.dump(backup) == original, "the backup is the database as it was before writing"

    again = run()
    assert again.exit_code == 0, again.output
    assert "计划写入 0 格（0 行）" in again.output
    rerun = run("--apply", "--out-dir", str(world.out))
    assert rerun.exit_code == 0, rerun.output
    assert "没有要写入的格" in rerun.output
    assert len(list(world.out.iterdir())) == 2, "a rerun takes no backup and writes no journal"
    assert world.dump() == after

    undo_preview = run("--undo", str(journal))
    assert undo_preview.exit_code == 0, undo_preview.output
    assert "可撤回 12 项，已是原值 0 项，之后被改动而保留 0 项" in undo_preview.output
    assert world.dump() == after

    undone = run("--undo", str(journal), "--apply")
    assert undone.exit_code == 0, undone.output
    assert "已撤回 12 项（14 格）" in undone.output
    assert world.dump() == original
    only(world.out, "*.journal.json.undo-*.json")

    twice = run("--undo", str(journal), "--apply")
    assert twice.exit_code == 0, twice.output
    assert "可撤回 0 项，已是原值 12 项" in twice.output
    assert world.dump() == original


def test_rows_in_flight_get_no_verdict_until_they_finish(world: World) -> None:
    populate(world)
    assert run("--apply", "--out-dir", str(world.out)).exit_code == 0
    after = world.dump()
    assert after["busy-nousage"]["usage_unmeasurable"] is None
    assert after["recent-nousage"]["usage_unmeasurable"] is None

    finished = ts(world.now - timedelta(hours=1))
    world.execute("UPDATE agents SET status = 'offline', last_active_at = ? WHERE id IN (?, ?)",
                  (finished, "busy-nousage", "recent-nousage"))
    later = run("--apply", "--out-dir", str(world.out))
    assert later.exit_code == 0, later.output
    assert "计划写入 2 格（2 行）" in later.output
    rows = world.dump()
    assert rows["busy-nousage"]["usage_unmeasurable"] == "no_usage_lines"
    assert rows["recent-nousage"]["usage_unmeasurable"] == "no_usage_lines"


def apply_with_writer_after_journal(world: World, monkeypatch: pytest.MonkeyPatch,
                                    writer: Callable[[], None]):
    """``--apply`` with ``writer`` landing between the journal and the write transaction."""
    write_journal = attribution_repair.write_journal

    def journal_then_concurrent_writer(path: Path, record: dict) -> None:
        write_journal(path, record)
        writer()

    monkeypatch.setattr(attribution_repair, "write_journal", journal_then_concurrent_writer)
    return run("--apply", "--out-dir", str(world.out))


def assert_rolled_back(world: World, result, original: dict) -> dict:
    """Nothing written, the backup of the untouched database gone, the journal says why."""
    assert result.exit_code == 1, result.output
    after = world.dump()
    for agent_id in original:
        for column in TARGETS:
            assert after[agent_id][column] == original[agent_id][column], \
                f"{agent_id}.{column} written despite the rollback"
    assert not list(world.out.glob("aiteam.db.bak-attribution-*")), "a rolled-back apply keeps no backup"
    assert "备份已删除" in result.output
    record = json.loads(only(world.out, "*.journal.json").read_text(encoding="utf-8"))
    assert record["status"] == "rolled_back"
    assert record["backup_discarded"] is True
    return record


def test_a_row_changed_after_planning_rolls_back_every_cell(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    populate(world)
    original = world.dump()
    measured = ts(world.now)

    # The shell row gets measured, so it is no longer a resume shell.
    result = apply_with_writer_after_journal(world, monkeypatch, lambda: world.execute(
        "UPDATE agents SET tokens_measured_at = ? WHERE id = 'shell'", (measured,)))
    assert "shell.tokens_measured_at" in result.output
    record = assert_rolled_back(world, result, original)
    assert world.dump()["shell"]["tokens_measured_at"] == measured
    assert {"agent_id": "shell", "field": "tokens_measured_at", "before": None, "now": measured} in record["mismatches"]
    refused = run("--undo", str(only(world.out, "*.journal.json")), "--apply")
    assert refused.exit_code == 1
    assert "整事务回滚" in refused.output


def test_backup_is_discarded_when_the_second_plan_finds_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows moved while the backup ran and nothing is left to write: no copy is kept."""
    world.team("team-s", "session-x")
    world.agent("shell", role="leader", harness="claude-code", session_id=SHELL_SID)
    original = world.dump()
    take_backup = attribution_repair.take_backup

    def backup_then_the_session_acts(db: Path, destination: Path) -> None:
        take_backup(db, destination)
        world.activity("shell")  # a tool call: no longer a resume shell

    monkeypatch.setattr(attribution_repair, "take_backup", backup_then_the_session_acts)
    result = run("--apply", "--out-dir", str(world.out))
    assert result.exit_code == 0, result.output
    assert "计划写入 0 格" in result.output
    assert "备份已删除" in result.output
    assert list(world.out.iterdir()) == [], "no backup and no journal without a write"
    assert world.dump() == original


def test_clock_marker_changed_after_planning_rolls_back(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The verdicts were reached under the planned clock; another marker voids them all."""
    populate(world)
    original = world.dump()
    result = apply_with_writer_after_journal(world, monkeypatch, lambda: world.user_version(0))
    assert "PRAGMA user_version 在计划之后变了" in result.output
    assert_rolled_back(world, result, original)


def test_a_leader_joining_a_duplicate_group_after_planning_rolls_back(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer measured Leader on the shared transcript would represent the group instead."""
    populate(world)
    original = world.dump()
    shared = original["dup-rep"]["transcript_path"]

    def joins() -> None:
        world.agent("dup-new", role="leader", harness="claude-code", session_id=DUP_SID, transcript_path=shared,
                    tokens_measured_at=ts(world.now), created_at=ts(world.now))

    result = apply_with_writer_after_journal(world, monkeypatch, joins)
    assert "共用 transcript 的 Leader 组变了" in result.output
    record = assert_rolled_back(world, result, original)
    assert {"group": shared, "before": ["dup-a", "dup-b", "dup-rep"],
            "now": ["dup-a", "dup-b", "dup-new", "dup-rep"]} in record["mismatches"]


@pytest.mark.parametrize(
    ("writes", "rolled_back"),
    [
        pytest.param({"status": "busy", "last_active_at": "now", "activity": True}, False, id="hook-activity"),
        pytest.param({"tokens_measured_at": None}, True, id="representative-key"),
    ],
)
def test_the_representative_is_compared_on_what_picks_it(
    world: World, monkeypatch: pytest.MonkeyPatch, writes: dict, rolled_back: bool,
) -> None:
    """The duplicate verdicts read the representative's grouping and ranking columns only.

    A live Leader representing its transcript is written by every hook call (status,
    last_active_at, a new activity row); that decides nothing and must not roll the
    apply back. Its ranking columns do.
    """
    expected = populate(world)
    original = world.dump()

    def hook_writes() -> None:
        columns = dict(writes)
        if columns.pop("activity", False):
            world.execute(
                "INSERT INTO agent_activities (id, agent_id, session_id, tool_name, input_summary, output_summary, "
                "timestamp, status) VALUES ('act-rep', 'dup-rep', 's', 'Bash', '', '', ?, 'completed')",
                (ts(world.now),))
        for column, value in columns.items():
            world.execute(f"UPDATE agents SET {column} = ? WHERE id = 'dup-rep'",  # noqa: S608
                          (ts(world.now) if value == "now" else value,))

    result = apply_with_writer_after_journal(world, monkeypatch, hook_writes)
    if rolled_back:
        assert "dup-rep.tokens_measured_at" in result.output
        assert_rolled_back(world, result, original)
        return
    assert result.exit_code == 0, result.output
    after = world.dump()
    for agent_id in original:
        for column in TARGETS:
            want = expected.get(agent_id, {}).get(column, original[agent_id][column])
            assert after[agent_id][column] == want, f"{agent_id}.{column}"
    assert after["dup-rep"]["status"] == "busy"


def test_harness_is_labelled_before_the_marks_read_it(world: World) -> None:
    """Codex rows whose harness is NULL must not meet the Claude Code rules.

    Two Codex Leaders without a harness label: one registered its real rollout as the
    transcript, one is a dual-UUIDv7 row with no transcript and no tool call. Judged
    with the NULL label, the Claude Code parser reads the rollout (no ``assistant``
    line, so ``no_usage_lines``) and the resume-shell rule takes the second row. With
    ``harness=codex`` labelled first, the Codex rules decide and neither verdict holds.
    """
    world.team("team-s", "session-x")
    rollout = world.codex / CODEX_ROLLOUT.relative_to(FIXTURES / "codex" / "rollouts" / "native")
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CODEX_ROLLOUT, rollout)
    assert unmeasurable_reason(str(rollout)) == "no_usage_lines", "the Claude Code reading of this rollout"
    world.agent("codex-leader", role="leader", session_id=CODEX_THREAD, transcript_path=str(rollout),
                last_active_at=ts(world.now - timedelta(hours=2)))
    world.agent("codex-shell", role="leader", session_id=THREAD_A, cc_tool_use_id=THREAD_B)
    original = world.dump()

    applied = run("--apply", "--out-dir", str(world.out))
    assert applied.exit_code == 0, applied.output
    assert "计划写入 2 格（2 行）" in applied.output
    assert_written(world.dump(), original, {"codex-leader": {"harness": "codex"}, "codex-shell": {"harness": "codex"}})


def test_a_row_with_an_unreadable_timestamp_gets_no_time_verdict(world: World) -> None:
    """A stored time nobody can parse is unknown, not now.

    ``tokens_measured_at`` of ``dup-garbled`` cannot be read. Read as now it would make
    that row the freshest measurement and the representative of its transcript, and the
    really measured ``dup-real`` would be excluded as its duplicate. The whole group is
    left for a later run instead; the harness label reads no time and is still written.
    """
    world.team("team-s", "session-x")
    shared = world.transcript(f"-p/{DUP_SID}.jsonl", PROMPT, BILLED)
    world.agent("dup-real", role="leader", harness="claude-code", session_id=DUP_SID, transcript_path=shared,
                tokens_measured_at=ts(world.now - timedelta(hours=1)))
    world.agent("dup-garbled", role="leader", session_id=YOUNG_SID, transcript_path=shared,
                tokens_measured_at="not a time")
    world.agent("dup-old", role="leader", harness="claude-code", transcript_path=shared)
    world.agent("done-garbled", harness="claude-code", last_active_at="garbled",
                transcript_path=world.transcript(f"-p/{DUP_SID}/subagents/agent-a1.jsonl", PROMPT))
    original = world.dump()

    applied = run("--apply", "--out-dir", str(world.out))
    assert applied.exit_code == 0, applied.output
    assert "时间值读不了、本次不下时间相关结论的行：2" in applied.output
    assert_written(world.dump(), original, {"dup-garbled": {"harness": "claude-code"}})


def test_unmarked_clock_writes_only_what_both_readings_agree_on(world: World) -> None:
    """``user_version`` 0: the stored value is read as UTC and as this host's wall clock.

    The row was last active five minutes ago on the local wall clock. Read as UTC it
    looks finished on hosts west of UTC; read as local time it is still in flight.
    Expectations follow from this host's time zone.
    """
    now = world.now
    world.team("team-s", "session-x")
    local_recent = (now - timedelta(minutes=5)).astimezone().replace(tzinfo=None)
    world.agent("clock-x", harness="claude-code", transcript_path=world.transcript("-p/x/agent-ax.jsonl", PROMPT),
                last_active_at=local_recent.isoformat(sep=" ", timespec="microseconds"))
    finished_as_utc = not is_in_flight("offline", local_recent.replace(tzinfo=UTC), now)
    finished_as_local = not is_in_flight("offline", local_recent.astimezone(UTC), now)
    assert not finished_as_local

    world.user_version(0)
    unmarked = run()
    assert unmarked.exit_code == 0, unmarked.output
    assert "user_version = 0（未标注）" in unmarked.output
    assert "计划写入 0 格" in unmarked.output

    world.user_version(attribution_repair.UTC_CLOCK_MARKER)
    marked = run()
    assert marked.exit_code == 0, marked.output
    assert ("计划写入 1 格" in marked.output) is finished_as_utc


def test_unknown_clock_is_refused_without_writing(world: World) -> None:
    populate(world)
    original = world.dump()
    world.user_version(7)
    for args in ((), ("--apply", "--out-dir", str(world.out))):
        result = run(*args)
        assert result.exit_code == 1, result.output
        assert "user_version = 7" in result.output
    assert world.dump() == original
    assert not world.out.exists()


def test_undo_keeps_decisions_changed_after_apply(world: World) -> None:
    expected = populate(world)
    original = world.dump()
    assert run("--apply", "--out-dir", str(world.out)).exit_code == 0
    journal = only(world.out, "*.journal.json")
    # Someone relabels one row; the API measures another (which voids its verdict).
    world.execute("UPDATE agents SET harness = 'codex' WHERE id = 'wf-sub'")
    world.execute("UPDATE agents SET usage_unmeasurable = NULL, tokens_measured_at = ? WHERE id = 'done-nousage'",
                  (ts(world.now),))

    result = run("--undo", str(journal), "--apply")
    assert result.exit_code == 0, result.output
    assert "可撤回 10 项，已是原值 1 项，之后被改动而保留 1 项" in result.output
    rows = world.dump()
    assert rows["wf-sub"]["harness"] == "codex"
    assert rows["done-nousage"]["tokens_measured_at"] is not None
    for agent_id in set(expected) - {"wf-sub", "done-nousage"}:
        for column in TARGETS:
            assert rows[agent_id][column] == original[agent_id][column], f"{agent_id}.{column}"


def test_undo_refuses_a_journal_of_another_database(world: World, tmp_path: Path) -> None:
    populate(world)
    assert run("--apply", "--out-dir", str(world.out)).exit_code == 0
    journal = only(world.out, "*.journal.json")
    other = World(tmp_path / "other")
    result = run("--undo", str(journal), "--apply", "--db", str(other.db))
    assert result.exit_code == 1
    assert "journal 属于另一个数据库" in result.output


def test_missing_columns_preview_but_refuse_to_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    schema = SCHEMA.read_text(encoding="utf-8").replace(" usage_unmeasurable VARCHAR(32), ", " ")
    assert "usage_unmeasurable" not in schema
    world = World(tmp_path, schema=schema)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(world.claude))
    monkeypatch.setenv("CODEX_HOME", str(world.codex))
    world.team("team-s", "session-x")
    world.agent("shell", role="leader", session_id=SHELL_SID)
    original = world.dump()

    preview = run("--db", str(world.db))
    assert preview.exit_code == 0, preview.output
    assert "agents 缺少列 usage_unmeasurable" in preview.output
    assert "计划写入 2 格" in preview.output
    refused = run("--db", str(world.db), "--apply", "--out-dir", str(world.out))
    assert refused.exit_code == 1
    assert "先让 API 在新版本上重启" in refused.output
    assert world.dump() == original
    assert not world.out.exists()


def test_database_built_by_init_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The schema a fresh install gets, with rows created through the repository."""
    import asyncio

    from aiteam.storage.connection import close_db
    from aiteam.storage.repository import StorageRepository

    db = tmp_path / "fresh.db"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))

    async def build() -> str:
        repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db}")
        await repo.init_db()
        team = await repo.create_team(name="session-6f1c2d3e", mode="coordinate")
        agent = await repo.create_agent(team_id=team.id, name="leader", role="leader", source="hook",
                                        session_id=SHELL_SID)
        await repo.update_agent(agent.id, status="offline")
        await close_db()
        return agent.id

    agent_id = asyncio.run(build())
    with closing(sqlite3.connect(db)) as con:
        two_days_ago = ts(datetime.now(UTC) - timedelta(days=2))
        con.execute("UPDATE agents SET created_at = ? WHERE id = ?", (two_days_ago, agent_id))
        con.commit()

    applied = run("--db", str(db), "--apply", "--out-dir", str(tmp_path / "out"))
    assert applied.exit_code == 0, applied.output
    with closing(sqlite3.connect(db)) as con:
        row = con.execute("SELECT harness, attribution_exclusion FROM agents WHERE id = ?", (agent_id,)).fetchone()
    assert row == ("claude-code", "resume_shell")


def test_python_dash_m_aiteam_runs_the_same_command(world: World) -> None:
    populate(world)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(attribution_repair.__file__).parents[2]),
                                                                       os.environ.get("PYTHONPATH")]))}
    result = subprocess.run(
        [sys.executable, "-m", "aiteam", "attribution", "repair", "--db", str(world.db)],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "计划写入 14 格（9 行）" in result.stdout


def test_utc_marker_matches_the_migration_script() -> None:
    import importlib.util

    script = Path(__file__).resolve().parents[3] / "scripts" / "migrate_timestamps_utc.py"
    spec = importlib.util.spec_from_file_location("migrate_timestamps_utc_for_marker", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert attribution_repair.UTC_CLOCK_MARKER == module.MIGRATION_MARKER
