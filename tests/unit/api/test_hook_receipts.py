"""Hook redelivery idempotency, asserted against the database across requests.

A redelivered event (same session, hook_event_name and tool_use_id) must be
answered with the first delivery's response and must not be recorded again, also
after a restart (new repository on the same file). Events without a tool_use_id
keep their old behaviour: every delivery is handled.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio

from aiteam.api import app as app_module
from aiteam.api import debug_log, deps, hook_receipts
from aiteam.api import event_bus as event_bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import hooks
from aiteam.api.ws.manager import ConnectionManager
from aiteam.clock import utc_now
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

SESSION = "synthetic-receipt-session"


def _pre(tool_use_id: str | None, marker: str, event: str = "PreToolUse") -> dict:
    payload = {
        "hook_event_name": event,
        "session_id": SESSION,
        "tool_name": "Bash",
        "tool_input": {"command": f"echo {marker}", "description": marker},
    }
    if tool_use_id is not None:
        payload["tool_use_id"] = tool_use_id
    return payload


class _Env:
    def __init__(self, database, database_url, monkeypatch) -> None:
        self.database = database
        self.database_url = database_url
        self.monkeypatch = monkeypatch

    async def client(self) -> httpx.AsyncClient:
        """A fresh app + repository on the same file: a restart as far as state goes."""
        app = app_module.create_app()
        repo = StorageRepository(db_url=self.database_url)
        await repo.init_db()
        bus = EventBus(repo=repo)
        translator = HookTranslator(repo=repo, event_bus=bus)
        app.dependency_overrides.update({
            deps.get_repository: lambda: repo,
            deps.get_event_bus: lambda: bus,
            deps.get_hook_translator: lambda: translator,
        })
        self.monkeypatch.setattr(deps, "_repository", repo)
        self.monkeypatch.setattr(deps, "_event_bus", bus)
        self.monkeypatch.setattr(deps, "_hook_translator", translator)
        self.repo = repo
        self.translator = translator
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def count(self, sql: str, *params) -> int:
        con = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return con.execute(sql, params).fetchone()[0]
        finally:
            con.close()

    def tool_use_events(self, marker: str) -> int:
        return self.count(
            "SELECT COUNT(*) FROM events WHERE type = 'cc.tool_use' AND data LIKE ?", f"%{marker}%",
        )


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "0")
    monkeypatch.setattr(debug_log, "setup_debug_log", lambda: None)
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.delenv(hooks.HOOK_RAW_DUMP_ENV, raising=False)
    database = tmp_path / "receipts.sqlite"
    database_url = f"sqlite+aiosqlite:///{database}"
    try:
        yield _Env(database, database_url, monkeypatch)
    finally:
        await get_engine(database_url).dispose()


@pytest.mark.asyncio
async def test_redelivery_is_answered_from_the_receipt_across_requests_and_restarts(env):
    async with await env.client() as client:
        first = await client.post("/api/hooks/event", json=_pre("toolu_dup_1", "mk-dup-1"))
        second = await client.post("/api/hooks/event", json=_pre("toolu_dup_1", "mk-dup-1"))
    assert first.status_code == second.status_code == 200
    assert "duplicate" not in first.json()
    assert second.json() == {**first.json(), "duplicate": True}
    assert env.tool_use_events("mk-dup-1") == 1
    assert env.count(
        "SELECT COUNT(*) FROM hook_event_receipts WHERE session_id = ? AND tool_use_id = ? "
        "AND hook_event_name = 'PreToolUse' AND response IS NOT NULL AND completed_at IS NOT NULL",
        SESSION, "toolu_dup_1",
    ) == 1

    # Restart: new app, new repository, same database file.
    async with await env.client() as client:
        third = await client.post("/api/hooks/event", json=_pre("toolu_dup_1", "mk-dup-1"))
    assert third.json() == {**first.json(), "duplicate": True}
    assert env.tool_use_events("mk-dup-1") == 1


@pytest.mark.asyncio
async def test_key_is_session_event_and_tool_use_id(env):
    async with await env.client() as client:
        await client.post("/api/hooks/event", json=_pre("toolu_k", "mk-key"))
        post = await client.post("/api/hooks/event", json=_pre("toolu_k", "mk-key", "PostToolUse"))
        other_session = {**_pre("toolu_k", "mk-key"), "session_id": "synthetic-other-session"}
        other = await client.post("/api/hooks/event", json=other_session)
    assert "duplicate" not in post.json()
    assert "duplicate" not in other.json()
    assert env.count("SELECT COUNT(*) FROM hook_event_receipts WHERE tool_use_id = 'toolu_k'") == 3
    assert env.tool_use_events("mk-key") == 2  # one Pre per session; Post records no cc.tool_use


@pytest.mark.asyncio
async def test_events_without_tool_use_id_are_not_deduplicated(env):
    async with await env.client() as client:
        for _ in range(2):
            response = await client.post("/api/hooks/event", json=_pre(None, "mk-noid"))
            assert "duplicate" not in response.json()
        blank = await client.post("/api/hooks/event", json=_pre("", "mk-noid"))
        assert "duplicate" not in blank.json()
    assert env.tool_use_events("mk-noid") == 3
    assert env.count("SELECT COUNT(*) FROM hook_event_receipts") == 0


@pytest.mark.asyncio
async def test_failed_delivery_releases_its_claim_so_a_retry_is_handled(env, monkeypatch):
    async with await env.client() as client:
        original = env.translator.handle_event
        calls = 0

        async def flaky(payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("database is locked")
            return await original(payload)

        monkeypatch.setattr(env.translator, "handle_event", flaky)
        with pytest.raises(RuntimeError):
            await client.post("/api/hooks/event", json=_pre("toolu_fail", "mk-fail"))
        assert env.count("SELECT COUNT(*) FROM hook_event_receipts WHERE tool_use_id = 'toolu_fail'") == 0
        retry = await client.post("/api/hooks/event", json=_pre("toolu_fail", "mk-fail"))
    assert retry.status_code == 200 and "duplicate" not in retry.json()
    assert env.tool_use_events("mk-fail") == 1


@pytest.mark.asyncio
async def test_in_flight_claim_answers_duplicate_and_a_stale_one_is_taken_over(env):
    async with await env.client() as client:
        repo = env.repo
        fresh = await repo.claim_hook_receipt(
            SESSION, "PreToolUse", "toolu_inflight", stale_before=utc_now() - timedelta(seconds=120),
        )
        assert fresh.claimed
        busy = await client.post("/api/hooks/event", json=_pre("toolu_inflight", "mk-inflight"))
        assert busy.json() == {"status": "duplicate", "reason": "in_flight", "duplicate": True}
        assert env.tool_use_events("mk-inflight") == 0

        # The owner died: once the claim is older than the stale window it is taken over.
        taken = await repo.claim_hook_receipt(
            SESSION, "PreToolUse", "toolu_inflight", stale_before=utc_now() + timedelta(seconds=1),
        )
        assert taken.claimed
        again = await repo.claim_hook_receipt(
            SESSION, "PreToolUse", "toolu_inflight", stale_before=utc_now() - timedelta(seconds=120),
        )
        assert (again.claimed, again.response) == (False, None)


@pytest.mark.asyncio
async def test_original_owner_cannot_release_or_complete_a_taken_over_claim(env):
    """Owner A overran the stale window, B took over, then A fails: B's claim stays."""
    async with await env.client():
        repo = env.repo
        key = (SESSION, "PreToolUse", "toolu_takeover")
        owner_a = await repo.claim_hook_receipt(*key, stale_before=utc_now() - timedelta(seconds=120))
        owner_b = await repo.claim_hook_receipt(*key, stale_before=utc_now() + timedelta(seconds=1))
        assert owner_a.claimed and owner_b.claimed and owner_a.token != owner_b.token

        assert not await repo.release_hook_receipt(*key, token=owner_a.token)
        third = await repo.claim_hook_receipt(*key, stale_before=utc_now() - timedelta(seconds=120))
        assert (third.claimed, third.response) == (False, None)  # B still owns it

        assert not await repo.complete_hook_receipt(*key, token=owner_a.token, response={"from": "a"})
        assert await repo.complete_hook_receipt(*key, token=owner_b.token, response={"from": "b"})
        answer = await repo.claim_hook_receipt(*key, stale_before=utc_now() - timedelta(seconds=120))
        assert (answer.claimed, answer.response) == (False, {"from": "b"})


@pytest.mark.asyncio
async def test_prune_removes_only_expired_receipts_in_bounded_batches(env):
    async with await env.client() as client:
        for index in range(5):
            await client.post("/api/hooks/event", json=_pre(f"toolu_keep_{index}", f"mk-keep-{index}"))
    assert await env.repo.prune_hook_receipts(
        utc_now() - hook_receipts.RECEIPT_RETENTION, limit=100,
    ) == 0
    future = utc_now() + timedelta(seconds=1)
    assert await env.repo.prune_hook_receipts(future, limit=2) == 2
    assert env.count("SELECT COUNT(*) FROM hook_event_receipts") == 3
    assert await env.repo.prune_hook_receipts(future, limit=100) == 3
    assert env.count("SELECT COUNT(*) FROM hook_event_receipts") == 0
    assert env.tool_use_events("mk-keep-0") == 1  # the events themselves are untouched


@pytest.mark.asyncio
async def test_reaper_hourly_prune_drains_the_backlog_in_batches(env, monkeypatch):
    import aiteam.api.state_reaper as reaper_mod

    async with await env.client() as client:
        for index in range(5):
            await client.post("/api/hooks/event", json=_pre(f"toolu_old_{index}", f"mk-old-{index}"))
    monkeypatch.setattr(reaper_mod, "HOOK_RECEIPT_PRUNE_BATCH", 2)
    monkeypatch.setattr(hook_receipts, "RECEIPT_RETENTION", timedelta(seconds=-1))
    batches = []
    original = env.repo.prune_hook_receipts

    async def counted(cutoff, *, limit):
        batches.append(limit)
        return await original(cutoff, limit=limit)

    monkeypatch.setattr(env.repo, "prune_hook_receipts", counted)
    assert await reaper_mod.StateReaper._prune_hook_receipts(env.repo) == 5
    assert batches == [2, 2, 2]
    assert env.count("SELECT COUNT(*) FROM hook_event_receipts") == 0


def test_receipt_key_requires_all_three_parts():
    assert hook_receipts.receipt_key({"session_id": "s", "hook_event_name": "PreToolUse",
                                      "tool_use_id": "t"}) == ("s", "PreToolUse", "t")
    for missing in ("session_id", "hook_event_name", "tool_use_id"):
        data = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_use_id": "t"}
        data[missing] = ""
        assert hook_receipts.receipt_key(data) is None
    assert hook_receipts.receipt_key({"session_id": "s", "hook_event_name": "E", "tool_use_id": 7}) is None
    # Codex re-sends carry meaning (newer source observations, completion replays).
    assert hook_receipts.receipt_key({"session_id": "s", "hook_event_name": "PreToolUse",
                                      "tool_use_id": "t", "harness": "codex"}) is None
