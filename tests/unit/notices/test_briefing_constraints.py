"""Pending decisions are for an absent user, answered once, and expire (09-23 ruling)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import update

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.clock import utc_now
from aiteam.services.notices import ledger
from aiteam.services.notices.detectors import decisions
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import LeaderBriefingModel
from aiteam.storage.repository import StorageRepository

from .conftest import request


@asynccontextmanager
async def _client(db_url):
    repository = StorageRepository(db_url=db_url)
    await repository.init_db()
    deps._repository = repository
    deps._event_bus = EventBus(repo=repository)
    app = create_app()

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app.router.lifespan_context = no_lifespan
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, repository
    finally:
        deps._repository = None
        deps._event_bus = None
        await close_db()


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'b.db'}"


async def _age(repository, briefing_id, days):
    async with get_session(repository._db_url) as session:
        await session.execute(
            update(LeaderBriefingModel).where(LeaderBriefingModel.id == briefing_id)
            .values(created_at=utc_now() - timedelta(days=days))
        )


async def test_pending_items_expire_after_fourteen_days_and_are_kept(db_url):
    async with _client(db_url) as (client, repository):
        old = (await client.post("/api/leader-briefings", json={"title": "old"})).json()
        young = (await client.post("/api/leader-briefings", json={"title": "young"})).json()
        await _age(repository, old["id"], 15)
        await _age(repository, young["id"], 13)
        pending = (await client.get("/api/leader-briefings")).json()["items"]
        assert [item["title"] for item in pending] == ["young"]
        expired = (await client.get("/api/leader-briefings?status=expired")).json()["items"]
        assert [item["id"] for item in expired] == [old["id"]]
        assert expired[0]["resolved_at"] is None and expired[0]["resolution"] == ""
        assert (await client.get("/api/leader-briefings?status=all")).json()["total"] == 2


async def test_resolved_items_never_expire(db_url):
    async with _client(db_url) as (client, repository):
        item = (await client.post("/api/leader-briefings", json={"title": "done"})).json()
        await client.put(f"/api/leader-briefings/{item['id']}/resolve", json={"resolution": "A"})
        await _age(repository, item["id"], 30)
        decisions._last_expiry.clear()
        rows = (await client.get("/api/leader-briefings?status=all")).json()["items"]
        assert rows[0]["status"] == "resolved"


async def test_real_only_drops_permission_denial_records(db_url):
    async with _client(db_url) as (client, _):
        await client.post("/api/leader-briefings", json={"title": "Agent denied: Bash"})
        await client.post("/api/leader-briefings", json={"title": "x", "tags": ["auto:permission-denied"]})
        await client.post("/api/leader-briefings", json={"title": "选型"})
        items = (await client.get("/api/leader-briefings?real_only=true")).json()["items"]
        assert [item["title"] for item in items] == ["选型"]
        assert (await client.get("/api/leader-briefings")).json()["total"] == 3


async def test_resolving_records_the_answer_as_a_decision(db_url):
    async with _client(db_url) as (client, _):
        item = (await client.post("/api/leader-briefings", json={"title": "数据库", "project_id": "p1"})).json()
        await client.put(f"/api/leader-briefings/{item['id']}/resolve", json={"resolution": "用 SQLite"})
    async with _client(db_url) as (client, _):
        events = (await client.get("/api/decisions?type=decision.briefing_resolved")).json()["data"]
        assert len(events) == 1
        assert events[0]["data"]["resolution"] == "用 SQLite" and events[0]["data"]["briefing_id"] == item["id"]
        assert events[0]["entity_id"] == item["id"]


async def test_dismiss_does_not_record_a_decision(db_url):
    async with _client(db_url) as (client, _):
        item = (await client.post("/api/leader-briefings", json={"title": "t"})).json()
        await client.put(f"/api/leader-briefings/{item['id']}/dismiss")
        events = (await client.get("/api/decisions?type=decision.briefing_resolved")).json()["data"]
        assert events == []


async def test_create_hints_to_ask_directly_when_the_user_was_just_active(db_url):
    async with _client(db_url) as (client, repository):
        quiet = (await client.post("/api/leader-briefings", json={"title": "q", "project_id": "p1"})).json()
        assert "hint" not in quiet and "user_present" not in quiet
        ledger.note_prompt(repository, "p1", utc_now() - timedelta(minutes=3))
        busy = (await client.post("/api/leader-briefings", json={"title": "q2", "project_id": "p1"})).json()
        assert busy["user_present"] is True and busy["hint"].startswith("The user is present, ask directly")
        assert "briefing_resolve" in busy["hint"]
        await client.put("/api/settings/language", json={"mode": "zh"})
        chinese = (await client.post("/api/leader-briefings", json={"title": "q2b", "project_id": "p1"})).json()
        assert chinese["hint"].startswith("用户在场，请直接问") and "3 分钟前" in chinese["hint"]
        other = (await client.post("/api/leader-briefings", json={"title": "q3", "project_id": "p2"})).json()
        assert "hint" not in other
        ledger.note_prompt(repository, "p1", utc_now() - timedelta(minutes=30))
        stale = (await client.post("/api/leader-briefings", json={"title": "q4", "project_id": "p1"})).json()
        assert "hint" not in stale


async def test_a_user_prompt_fetch_marks_presence(repo):
    await ledger.pending(repo, request(event="UserPromptSubmit").model_copy(update={"project_id": "p9"}),
                         registry=[])
    assert ledger.user_recently_active(repo, "p9") is not None
    assert ledger.user_recently_active(repo, "other") is None


async def test_pending_count_uses_the_same_rule_and_expires_first(repo):
    now = utc_now()
    await repo.create_briefing(title="Agent denied: Edit")
    await repo.create_briefing(title="auto", tags=["auto:permission-denied"])
    real = await repo.create_briefing(title="真问题", project_id="p1")
    await repo.create_briefing(title="别的项目", project_id="p2")
    old = await repo.create_briefing(title="过期", project_id="p1")
    await _age(repo, old.id, 20)
    items = await decisions.pending_decisions(repo, "p1", now)
    assert [item.id for item in items] == [real.id]
    assert (await repo.list_briefings(status="expired"))[0].id == old.id
