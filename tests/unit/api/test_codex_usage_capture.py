"""Real file DB lifecycle capture and atomic stale parse protection."""

import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.services import codex_rollout_usage
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    db = tmp_path / "capture.sqlite"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db}")
    await repo.init_db()
    await repo.create_project(name="Usage", root_path=str(tmp_path))
    translator = HookTranslator(repo, EventBus(repo))
    yield repo, translator, db, tmp_path
    await translator.drain(timeout=10)
    await close_db()


def log(root, sid, child=False):
    folder = root / ".codex/sessions"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"rollout-date-{sid}.jsonl"
    rows = [
        dict(type="session_meta", timestamp="2026-09-30T00:00:00Z", payload=dict(id=sid, source="cli")),
        dict(
            type="token_usage_record",
            timestamp="2026-09-30T00:00:01Z",
            payload=dict(
                thread_id=sid,
                response_id="r",
                usage=dict(
                    input_tokens=100,
                    cached_input_tokens=60,
                    cache_write_input_tokens=0,
                    output_tokens=20,
                    reasoning_output_tokens=5,
                    total_tokens=120,
                ),
            ),
        ),
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


def event(root, sid, path, event="SessionStart", parent=None):
    root_id = parent or sid
    return dict(
        harness="codex",
        session_id=root_id,
        agent_id=sid if parent else "",
        cwd=str(root),
        hook_event_name=event,
        codex_observation=dict(
            version=1,
            actor_id=sid,
            root_session_id=root_id,
            parent_thread_id=parent,
            source_kind="subagent" if parent else "cli",
            agent_name="Worker" if parent else "",
            agent_role="member" if parent else "",
            model="gpt-fixture",
            transcript_path=str(path),
            cli_version="0.test",
        ),
    )


async def test_leader_and_child_lifecycle_subprocess_persist_distinct_owned_usage(setup):
    repo, translator, db, root = setup
    sid = str(uuid4())
    child = str(uuid4())
    p = log(root, sid)
    cp = log(root, child)
    await translator.handle_event(event(root, sid, p))
    second = await translator.handle_event(event(root, child, cp, parent=sid, event="SubagentStart"))
    await translator.drain(timeout=10)
    # New connection sees committed values, proving the public lifecycle path.
    with sqlite3.connect(db) as con:
        rows = con.execute(
            "select cc_tool_use_id,input_tokens,output_tokens,cache_read_tokens,reasoning_output_tokens,"
            "tokens_source from agents order by cc_tool_use_id"
        ).fetchall()
    assert len(rows) == 2
    assert {r[0] for r in rows} == {sid, child}
    assert all(r[1:] == (40, 20, 60, 5, "codex_rollout") for r in rows)
    appended = dict(
        type="token_usage_record",
        timestamp="2026-09-30T00:00:02Z",
        payload=dict(
            thread_id=child,
            response_id="r2",
            usage=dict(
                input_tokens=200,
                cached_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=30,
                reasoning_output_tokens=10,
                total_tokens=230,
            ),
        ),
    )
    with cp.open("a") as stream:
        stream.write(json.dumps(appended) + "\n")
    await translator.handle_event(event(root, child, cp, parent=sid, event="Stop"))
    await translator.drain(timeout=10)
    with sqlite3.connect(db) as con:
        observed = con.execute(
            "select input_tokens, output_tokens, reasoning_output_tokens from agents where id=?", (second["agent_id"],)
        ).fetchone()
        assert observed == (240, 50, 15)


async def test_parse_rebinding_and_concurrent_measurement_are_compare_and_set(setup, monkeypatch):
    repo, translator, db, root = setup
    sid = str(uuid4())
    p = log(root, sid)
    # Create actor without scheduling a first parse.
    team = await repo.create_team(name="capture", mode="coordinate")
    actor = await repo.create_agent(team_id=team.id, name="Leader", role="leader", session_id=sid, cc_tool_use_id=sid)
    await repo.update_agent(actor.id, harness="codex", transcript_path=str(p))
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        return dict(
            input_tokens=40,
            output_tokens=20,
            cache_creation_tokens=0,
            cache_read_tokens=60,
            reasoning_output_tokens=5,
            transcript_path=str(p),
        )

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_usage", delayed)
    task = asyncio.create_task(translator._capture_codex_usage(actor.id))
    await started.wait()
    await repo.update_agent(actor.id, tokens_measured_at=utc_now(), input_tokens=777, tokens_source="codex_rollout")
    release.set()
    await task
    with sqlite3.connect(db) as con:
        assert con.execute("select input_tokens from agents where id=?", (actor.id,)).fetchone()[0] == 777
    started.clear()
    release.clear()
    task = asyncio.create_task(translator._capture_codex_usage(actor.id))
    await started.wait()
    await repo.update_agent(actor.id, cc_tool_use_id=str(uuid4()))
    release.set()
    await task
    with sqlite3.connect(db) as con:
        assert con.execute("select input_tokens from agents where id=?", (actor.id,)).fetchone()[0] == 777


async def test_stop_throttles_and_missing_usage_remains_unmeasured(setup, monkeypatch):
    repo, translator, db, root = setup
    sid = str(uuid4())
    p = log(root, sid)
    calls = 0

    async def empty(*args, **kwargs):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_usage", empty)
    first = await translator.handle_event(event(root, sid, p))
    await translator.drain(timeout=10)
    for _ in range(5):
        await translator.handle_event(event(root, sid, p, event="Stop"))
    await translator.drain(timeout=10)
    assert calls >= 2  # Empty Start releases its claim; short Stop may retry.
    with sqlite3.connect(db) as con:
        assert (
            con.execute("select tokens_measured_at from agents where id=?", (first["agent_id"],)).fetchone()[0] is None
        )


async def test_recovered_old_path_does_not_override_new_observation(setup, monkeypatch):
    repo, translator, db, root = setup
    sid = str(uuid4())
    p = log(root, sid)

    async def relocated(*args, **kwargs):
        return dict(
            input_tokens=40,
            output_tokens=20,
            cache_creation_tokens=0,
            cache_read_tokens=60,
            reasoning_output_tokens=5,
            transcript_path=str(root / "archive.jsonl"),
        )

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_usage", relocated)
    first = await translator.handle_event(event(root, sid, p))
    await translator.drain(timeout=10)
    await translator.handle_event(event(root, sid, p, event="PreToolUse"))
    assert (await repo.get_agent(first["agent_id"])).transcript_path == str(root / "archive.jsonl")
    newer = root / "new-native.jsonl"
    await translator.handle_event(event(root, sid, newer, event="PreToolUse"))
    assert (await repo.get_agent(first["agent_id"])).transcript_path == str(newer)


async def test_meta_only_start_then_short_stop_captures_first_real_usage(setup):
    repo, translator, db, root = setup
    sid = str(uuid4())
    p = log(root, sid)
    complete = p.read_text()
    p.write_text(complete.splitlines()[0] + "\n")
    first = await translator.handle_event(event(root, sid, p))
    await translator.drain(timeout=10)
    with sqlite3.connect(db) as con:
        assert (
            con.execute("select tokens_measured_at from agents where id=?", (first["agent_id"],)).fetchone()[0] is None
        )
    p.write_text(complete)
    await translator.handle_event(event(root, sid, p, event="Stop"))
    await translator.drain(timeout=10)
    with sqlite3.connect(db) as con:
        assert con.execute("select input_tokens from agents where id=?", (first["agent_id"],)).fetchone()[0] == 40


async def test_successful_start_throttles_repeated_stop(setup, monkeypatch):
    repo, translator, db, root = setup
    sid = str(uuid4())
    p = log(root, sid)
    calls = 0

    async def measured(*args, **kwargs):
        nonlocal calls
        calls += 1
        return dict(
            input_tokens=40,
            output_tokens=20,
            cache_creation_tokens=0,
            cache_read_tokens=60,
            reasoning_output_tokens=5,
            transcript_path=str(p),
        )

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_usage", measured)
    await translator.handle_event(event(root, sid, p))
    await translator.drain(timeout=10)
    for _ in range(5):
        await translator.handle_event(event(root, sid, p, event="Stop"))
    await translator.drain(timeout=10)
    assert calls == 1


async def test_changed_transcript_path_invalidates_pending_capture(setup, monkeypatch):
    repo, translator, db, root = setup
    sid = str(uuid4())
    p = log(root, sid)
    actor = await repo.create_agent(team_id="capture", name="Leader", role="leader", session_id=sid, cc_tool_use_id=sid)
    await repo.update_agent(actor.id, harness="codex", transcript_path=str(p))
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        return dict(
            input_tokens=40,
            output_tokens=20,
            cache_creation_tokens=0,
            cache_read_tokens=60,
            reasoning_output_tokens=5,
            transcript_path=str(p),
        )

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_usage", delayed)
    task = asyncio.create_task(translator._capture_codex_usage(actor.id))
    await started.wait()
    newer = str(root / "new-rollout.jsonl")
    await repo.update_agent(actor.id, transcript_path=newer)
    release.set()
    await task
    with sqlite3.connect(db) as con:
        row = con.execute(
            "select transcript_path, input_tokens, tokens_measured_at from agents where id=?", (actor.id,)
        ).fetchone()
    assert row == (newer, None, None)


async def test_sql_harness_guard_independently_refuses_non_codex_rows(setup):
    repo, translator, db, root = setup
    sid = str(uuid4())
    p = log(root, sid)
    actor = await repo.create_agent(team_id="capture", name="CC", role="leader", session_id=sid, cc_tool_use_id=sid)
    await repo.update_agent(actor.id, harness="claude-code", transcript_path=str(p))
    written = await repo.update_codex_usage_if_current(
        actor.id,
        expected_session_id=sid,
        expected_cc_tool_use_id=sid,
        expected_transcript_path=str(p),
        expected_measured_at=None,
        input_tokens=40,
        output_tokens=20,
        cache_creation_tokens=0,
        cache_read_tokens=60,
        reasoning_output_tokens=5,
        tokens_measured_at=utc_now(),
        transcript_path=str(p),
    )
    assert written is False
    with sqlite3.connect(db) as con:
        row = con.execute(
            "select harness,input_tokens,tokens_measured_at from agents where id=?", (actor.id,)
        ).fetchone()
    assert row == ("claude-code", None, None)


async def test_python_harness_guard_refuses_parsing_before_sql_guard(setup, monkeypatch):
    repo, translator, db, root = setup
    sid = str(uuid4())
    p = log(root, sid)
    actor = await repo.create_agent(team_id="capture", name="CC", role="leader", session_id=sid, cc_tool_use_id=sid)
    await repo.update_agent(actor.id, harness="claude-code", transcript_path=str(p))
    attempts = []

    async def unexpected_parse(*args, **kwargs):
        attempts.append(kwargs["expected_session_id"])
        return dict(
            input_tokens=40,
            output_tokens=20,
            cache_creation_tokens=0,
            cache_read_tokens=60,
            reasoning_output_tokens=5,
            transcript_path=str(p),
        )

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_usage", unexpected_parse)
    await translator._capture_codex_usage(actor.id)
    assert attempts == []
    with sqlite3.connect(db) as con:
        assert con.execute("select tokens_measured_at from agents where id=?", (actor.id,)).fetchone()[0] is None
