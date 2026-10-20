"""Codex Stop final captures through production HTTP ingest and a real file DB.

The parser wrappers only control arrival order; all usage is parsed in the real
subprocess and persisted through the production compare-and-set repository path.
"""

import asyncio
import json
import sqlite3
from uuid import uuid4

import httpx
import pytest_asyncio
from fastapi import FastAPI

from aiteam.api.deps import get_hook_translator
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import hooks
from aiteam.services import codex_rollout_usage
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.delenv(hooks.HOOK_RAW_DUMP_ENV, raising=False)
    database = tmp_path / "stop.sqlite"
    url = f"sqlite+aiosqlite:///{database}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    await repo.create_project(name="Stop capture", root_path=str(tmp_path))
    translator = HookTranslator(repo, EventBus(repo))
    app = FastAPI()
    app.include_router(hooks.router)
    app.dependency_overrides[get_hook_translator] = lambda: translator
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        try:
            yield client, translator, database, tmp_path
        finally:
            await translator.drain(10)
            await get_engine(url).dispose()


def usage(sid, response, inp=100, out=20, cache=60):
    return dict(type="token_usage_record", timestamp="2026-09-30T00:00:01Z", payload=dict(
        thread_id=sid, response_id=response, usage=dict(input_tokens=inp,
            cached_input_tokens=cache, cache_write_input_tokens=0, output_tokens=out,
            reasoning_output_tokens=5, total_tokens=inp + out),
    ))


def rollout(root, sid):
    path = root / ".codex/sessions" / f"rollout-date-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(type="session_meta", timestamp="2026-09-30T00:00:00Z",
        payload=dict(id=sid, source="cli"))) + "\n" + json.dumps(usage(sid, "r1")) + "\n")
    return path


def append(path, sid, response="r2"):
    with path.open("a") as stream:
        stream.write(json.dumps(usage(sid, response, inp=200, out=30, cache=0)) + "\n")


async def post(client, root, sid, path, name):
    payload = dict(harness="codex", session_id=sid, cwd=str(root), hook_event_name=name,
        codex_observation=dict(version=1, actor_id=sid, root_session_id=sid,
            parent_thread_id=None, source_kind="cli", agent_name="", agent_role="",
            model="gpt-fixture", transcript_path=str(path), cli_version="0.test"))
    response = await asyncio.wait_for(client.post("/api/hooks/event", json=payload), 5)
    assert response.status_code == 200, response.text
    return response.json()


def measured(database, sid):
    # A new connection proves that a completed HTTP request is followed by a real commit.
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT input_tokens,output_tokens FROM agents WHERE cc_tool_use_id=?",
            (sid,)).fetchone()


async def test_two_short_stops_persist_the_last_turn_without_another_lifecycle_event(stack):
    client, translator, database, root = stack
    sid = str(uuid4())
    path = rollout(root, sid)
    await post(client, root, sid, path, "SessionStart")
    assert await translator.drain(10)
    await post(client, root, sid, path, "Stop")
    assert await translator.drain(10)
    assert measured(database, sid) == (40, 20)
    append(path, sid)
    await post(client, root, sid, path, "Stop")
    assert await translator.drain(10)
    assert measured(database, sid) == (240, 50)


async def test_stop_while_start_is_in_flight_coalesces_and_preserves_a_final_capture(stack, monkeypatch):
    client, translator, database, root = stack
    sid = str(uuid4())
    path = rollout(root, sid)
    real_read = codex_rollout_usage.read_codex_rollout_state
    started, release = asyncio.Event(), asyncio.Event()
    calls = active = maximum = 0

    async def gated(*args, **kwargs):
        nonlocal calls, active, maximum
        calls += 1
        active += 1
        maximum = max(maximum, active)
        try:
            snapshot = await real_read(*args, **kwargs)
            if calls == 1:
                started.set()
                await release.wait()
            return snapshot
        finally:
            active -= 1

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_state", gated)
    try:
        await post(client, root, sid, path, "SessionStart")
        await asyncio.wait_for(started.wait(), 10)
        append(path, sid)
        # Receipts return while the old snapshot is still in flight. All twelve
        # Stops must collapse into one later job, rather than twelve parallel reads.
        for _ in range(12):
            await post(client, root, sid, path, "Stop")
        assert calls == 1
        assert measured(database, sid) == (None, None)
    finally:
        release.set()
    assert await translator.drain(10)
    assert measured(database, sid) == (240, 50)
    assert maximum == 1
    assert calls == 2  # Start, Stop snapshot; the file did not change, so no tail parse.
    assert translator._background.in_flight == 0


async def test_stop_tail_reads_bytes_written_after_its_first_parse_and_drain_waits(stack, monkeypatch):
    client, translator, database, root = stack
    sid = str(uuid4())
    path = rollout(root, sid)
    await post(client, root, sid, path, "SessionStart")
    assert await translator.drain(10)
    real_read = codex_rollout_usage.read_codex_rollout_state
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def gated(*args, **kwargs):
        nonlocal calls
        calls += 1
        snapshot = await real_read(*args, **kwargs)
        if calls == 1:
            started.set()
            await release.wait()
        return snapshot

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_state", gated)
    try:
        await post(client, root, sid, path, "Stop")
        await asyncio.wait_for(started.wait(), 10)
        append(path, sid)
        draining = asyncio.create_task(translator.drain(10))
        await asyncio.sleep(0)
        assert not draining.done()
    finally:
        release.set()
    assert await draining
    assert measured(database, sid) == (240, 50)
    assert calls == 2
    assert translator._background.in_flight == 0


async def test_continuously_growing_rollout_does_not_create_unbounded_tail_reads(stack, monkeypatch):
    client, translator, database, root = stack
    sid = str(uuid4())
    path = rollout(root, sid)
    await post(client, root, sid, path, "SessionStart")
    assert await translator.drain(10)
    real_read = codex_rollout_usage.read_codex_rollout_state
    calls = 0

    async def growing(*args, **kwargs):
        nonlocal calls
        calls += 1
        snapshot = await real_read(*args, **kwargs)
        append(path, sid, response=f"late-{calls}")
        return snapshot

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_state", growing)
    await post(client, root, sid, path, "Stop")
    assert await translator.drain(10)
    assert calls == 2
    # The second late append is outside the bounded capture; no absolute finality
    # claim is made for arbitrarily delayed writes after the final parse.
    assert measured(database, sid) == (240, 50)
    assert translator._background.in_flight == 0


async def test_unchanged_rollout_skips_the_stop_tail_parse(stack, monkeypatch):
    client, translator, database, root = stack
    sid = str(uuid4())
    path = rollout(root, sid)
    await post(client, root, sid, path, "SessionStart")
    assert await translator.drain(10)
    real_read = codex_rollout_usage.read_codex_rollout_state
    calls = 0

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real_read(*args, **kwargs)

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_state", counted)
    await post(client, root, sid, path, "Stop")
    assert await translator.drain(10)
    assert calls == 1  # Same (size, mtime_ns) after the tail window: nothing new to parse.
    assert measured(database, sid) == (40, 20)


async def test_queued_final_capture_is_not_replaced_by_a_later_nonfinal_capture(stack, monkeypatch):
    client, translator, database, root = stack
    sid = str(uuid4())
    path = rollout(root, sid)
    real_read = codex_rollout_usage.read_codex_rollout_state
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def gated(*args, **kwargs):
        nonlocal calls
        calls += 1
        snapshot = await real_read(*args, **kwargs)
        if calls == 1:
            started.set()
            await release.wait()
        elif calls == 2:
            append(path, sid)  # The last turn lands after the queued Stop's first parse.
        return snapshot

    monkeypatch.setattr(codex_rollout_usage, "read_codex_rollout_state", gated)
    try:
        await post(client, root, sid, path, "SessionStart")
        await asyncio.wait_for(started.wait(), 10)
        await post(client, root, sid, path, "Stop")
        await post(client, root, sid, path, "SessionStart")
    finally:
        release.set()
    assert await translator.drain(10)
    assert calls == 3  # Start, then the retained Stop job: snapshot and its tail.
    assert measured(database, sid) == (240, 50)
    assert translator._background.in_flight == 0
