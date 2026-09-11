"""Real sender, HTTP hooks, and persistent tool spans across API restarts."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import sys
from contextlib import asynccontextmanager, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from aiteam.api.deps import get_hook_translator
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import hooks
from aiteam.storage import repository as repository_module
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "plugin/harness/codex/hooks/send_event_codex.py"
SESSION = "codex-completion-test"
TOOL = "mcp__cua_repl__js"


def payload(event: str, call_id: str | None = "call-native-one") -> dict:
    value = {
        "harness": "codex", "session_id": SESSION, "hook_event_name": event,
        "tool_name": TOOL, "tool_input": {"description": call_id or "unidentified"},
        "tool_response": {"result": "observed result"}, "turn_id": "turn-native-one",
    }
    if call_id:
        value["tool_call_id"] = call_id
    return value


@pytest.fixture
async def repo(tmp_path):
    repository = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'events.sqlite3'}")
    await repository.init_db()
    team = await repository.create_team(name="completion-test", mode="coordinate")
    await repository.create_agent(
        team_id=team.id, name="team-lead", role="leader", session_id=SESSION,
        source="api", status="busy", harness="codex",
    )
    try:
        yield repository
    finally:
        await close_db()


@asynccontextmanager
async def api(repository):
    translator = HookTranslator(repo=repository, event_bus=EventBus(repository))
    app = FastAPI()
    app.dependency_overrides[get_hook_translator] = lambda: translator
    app.include_router(hooks.router)
    control = {"reject_completion": False, "completions": 0, "first_ack_delay": 0}

    @app.middleware("http")
    async def transient_failure(request, call_next):
        body = await request.json()
        delay = 0
        if body.get("hook_event_name") == "PostToolUse":
            control["completions"] += 1
            if control["reject_completion"]:
                return JSONResponse({"error": "temporary test failure"}, status_code=503)
            delay = control["first_ack_delay"] if control["completions"] == 1 else 0
        response = await call_next(request)
        if delay:
            await asyncio.sleep(delay)
        return response

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    server.capture_signals = nullcontext
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}", control
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 5)
        except TimeoutError:
            serving.cancel()
            with suppress(asyncio.CancelledError):
                await serving
        listener.close()


async def send_hook(state_dir: Path, url: str, value: dict) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(HOOK), value["hook_event_name"],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "AITEAM_CODEX_STATE_DIR": str(state_dir),
             "AITEAM_API_URL": url, "PYTHONDONTWRITEBYTECODE": "1",
             "NO_PROXY": "127.0.0.1,localhost"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(value).encode()), 5,
        )
        assert process.returncode == 0, stderr.decode()
        assert stdout == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_completion_updates_original_span_after_api_restart_and_duplicate(repo):
    async with api(repo) as (url, _), httpx.AsyncClient(base_url=url, trust_env=False) as client:
        response = await client.post("/api/hooks/event", json=payload("PreToolUse"))
        assert response.status_code == 200
        before, = await repo.list_activities_by_session(SESSION)
        assert before.status == "running"
    async with api(repo) as (url, _), httpx.AsyncClient(base_url=url, trust_env=False) as client:
        for event in ("PostToolUse", "PostToolUse", "PreToolUse"):
            response = await client.post("/api/hooks/event", json=payload(event))
            assert response.status_code == 200
        after, = await repo.list_activities_by_session(SESSION)
        assert after.id == before.id
        assert after.status == "completed"


async def test_real_sender_recovers_failed_completion_on_later_hook(repo, tmp_path):
    async with api(repo) as (url, control):
        await send_hook(tmp_path / "sender", url, payload("PreToolUse"))
        control["reject_completion"] = True
        await send_hook(tmp_path / "sender", url, payload("PostToolUse"))
        before, = await repo.list_activities_by_session(SESSION)
        assert before.status == "running"
        with sqlite3.connect(tmp_path / "sender" / "completion-outbox.sqlite3") as outbox:
            queued, = outbox.execute("SELECT payload FROM completions").fetchone()
        assert "observed result" not in queued and "tool_input" not in queued
        control["reject_completion"] = False
        await send_hook(tmp_path / "sender", url, payload("PreToolUse", "call-native-two"))
        activities = await repo.list_activities_by_session(SESSION)
        original = next(activity for activity in activities if activity.id == before.id)
        assert original.status == "completed"
        assert len(activities) == 2
        assert control["completions"] >= 2


@pytest.mark.parametrize("harness,expected", [("codex", "running"), (None, "completed")])
async def test_missing_call_id_keeps_codex_unknown_and_cc_legacy_unchanged(repo, harness, expected):
    async with api(repo) as (url, _), httpx.AsyncClient(base_url=url, trust_env=False) as client:
        for event in ("PreToolUse", "PostToolUse"):
            body = payload(event, None)
            if harness is None:
                body.pop("harness")
            await client.post("/api/hooks/event", json=body)
    activity, = await repo.list_activities_by_session(SESSION)
    assert activity.status == expected


async def test_real_sender_lost_ack_replays_exact_tool_use_id_without_duplicate(repo, tmp_path):
    async with api(repo) as (url, control):
        control["first_ack_delay"] = 1.7
        for event in ("PreToolUse", "PostToolUse"):
            body = payload(event)
            body["tool_use_id"] = body.pop("tool_call_id")
            await send_hook(tmp_path / "sender", url, body)
        activity, = await repo.list_activities_by_session(SESSION)
        assert control["completions"] == 1
        await send_hook(tmp_path / "sender", url, payload("PreToolUse", "later-hook"))
        assert activity.status == "completed"
        assert activity.output_summary == "{'result': 'observed result'}"
        assert control["completions"] == 2
        with sqlite3.connect(tmp_path / "sender" / "completion-outbox.sqlite3") as outbox:
            assert outbox.execute("SELECT COUNT(*) FROM completions").fetchone() == (0,)


async def test_primary_retains_one_second_headroom_and_does_not_retry_itself(repo, tmp_path):
    async with api(repo) as (url, control):
        control["first_ack_delay"] = 1.0
        await send_hook(tmp_path / "sender", url, payload("PreToolUse"))
        await send_hook(tmp_path / "sender", url, payload("PostToolUse"))
        assert control["completions"] == 1
        with sqlite3.connect(tmp_path / "sender" / "completion-outbox.sqlite3") as outbox:
            assert outbox.execute("SELECT COUNT(*) FROM completions").fetchone() == (0,)
        control["reject_completion"] = True
        await send_hook(tmp_path / "sender", url, payload("PostToolUse", "deferred"))
        assert control["completions"] == 2
        with sqlite3.connect(tmp_path / "sender" / "completion-outbox.sqlite3") as outbox:
            assert outbox.execute("SELECT attempts FROM completions").fetchall() == [(1,)]


async def test_delayed_replay_preserves_observation_not_fabricated_duration(repo, tmp_path, monkeypatch):
    async with api(repo) as (url, control):
        await send_hook(tmp_path / "sender", url, payload("PreToolUse"))
        before, = await repo.list_activities_by_session(SESSION)
        control["reject_completion"] = True
        await send_hook(tmp_path / "sender", url, payload("PostToolUse"))
        with sqlite3.connect(tmp_path / "sender" / "completion-outbox.sqlite3") as outbox:
            encoded, = outbox.execute("SELECT payload FROM completions").fetchone()
        original_observation = json.loads(encoded)["_codex_completion_observed_at"]
        observed_at = datetime.fromisoformat(original_observation)
        assert observed_at.tzinfo is not None and observed_at <= datetime.now(UTC)
        delayed_receive = datetime.now(UTC) + timedelta(minutes=5)
        monkeypatch.setattr(repository_module, "utc_now", lambda: delayed_receive)
        control["reject_completion"] = False
        await send_hook(tmp_path / "sender", url, payload("PreToolUse", "later-hook"))
        activity = next(item for item in await repo.list_activities_by_session(SESSION)
                        if item.id == before.id)
        assert activity.status == "completed"
        assert activity.duration_ms is None
        completion, = await repo.list_events(event_type="cc.tool_complete")
        assert completion.data["completion_observed_at"] == original_observation
