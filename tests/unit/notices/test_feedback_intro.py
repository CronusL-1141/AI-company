"""E32 feedback_intro: once per host, only while this process registers the feedback tools.

Through the real routes (/api/notices/pending, the notice detail), with a new app
instance and repository between sessions, as test_notices_api does.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.services.notices import ledger
from aiteam.services.notices.catalog import CATALOG
from aiteam.services.notices.detectors.feedback_intro import FeedbackIntroDetector
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

ENGLISH = CATALOG["feedback_intro"].variants[""].user["en"]


@asynccontextmanager
async def _client(db_url: str):
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
            yield client
    finally:
        deps._repository = None
        deps._event_bus = None
    await close_db()  # the next app reads from disk


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"


@pytest.fixture(autouse=True)
def unhurried(monkeypatch):
    # Under test load the production budgets (0.3 s detector, 1.5 s fetch) may time out,
    # which reads as "no data": the line under test would just not appear.
    monkeypatch.setattr(FeedbackIntroDetector, "timeout_s", 10.0)
    monkeypatch.setitem(ledger.DEADLINE_S, "SessionStart", 10.0)
    monkeypatch.delenv("AITEAM_TOOLSETS", raising=False)
    monkeypatch.delenv("AITEAM_READONLY", raising=False)


def _start(host: str, session: str, emitted=()) -> dict:
    return {"host": host, "event": "SessionStart", "source": "startup", "session_id": session, "cwd": "",
            "facts": {"entrypoint": "cli", "fallback_language": "en", "emitted": list(emitted)}}


async def _session_start(db_url: str, host: str, session: str) -> dict:
    async with _client(db_url) as client:
        answer = (await client.post("/api/notices/pending", json=_start(host, session))).json()
        if answer["delivery_ids"]:  # the hook reports what it wrote
            await client.post("/api/notices/pending", json={
                **_start(host, session, answer["delivery_ids"]), "event": "UserPromptSubmit", "source": ""})
    return answer


async def _status(db_url: str, key: str) -> str | None:
    async with _client(db_url) as client:
        response = await client.get(f"/api/notices/{key}")
    return response.json()["notice"]["status"] if response.status_code == 200 else None


async def test_said_once_per_host_then_gone_from_the_lists(db_url) -> None:
    first = await _session_start(db_url, "cc", "s1")
    assert ENGLISH in first["user_text"] and "feedback_draft" in first["model_text"]
    assert ENGLISH not in (await _session_start(db_url, "cc", "s2"))["user_text"]
    assert await _status(db_url, "feedback_intro:cc") == "cleared"
    # The other host has not been told yet.
    assert ENGLISH in (await _session_start(db_url, "codex", "c1"))["user_text"]
    assert ENGLISH not in (await _session_start(db_url, "codex", "c2"))["user_text"]


@pytest.mark.parametrize(("toolsets", "readonly"), [("default", ""), ("task,team", ""), ("", "1")],
                         ids=["default-profile", "groups-without-feedback", "read-only"])
async def test_not_said_where_the_tools_are_not_registered(db_url, monkeypatch, toolsets, readonly) -> None:
    if toolsets:
        monkeypatch.setenv("AITEAM_TOOLSETS", toolsets)
    if readonly:
        monkeypatch.setenv("AITEAM_READONLY", readonly)
    assert ENGLISH not in (await _session_start(db_url, "cc", "s1"))["user_text"]
    assert await _status(db_url, "feedback_intro:cc") is None


async def test_said_when_the_group_is_added_to_the_default_profile(db_url, monkeypatch) -> None:
    monkeypatch.setenv("AITEAM_TOOLSETS", "default,feedback")
    assert ENGLISH in (await _session_start(db_url, "cc", "s1"))["user_text"]


async def test_an_untold_line_clears_when_the_tools_go_away(db_url, monkeypatch) -> None:
    async with _client(db_url) as client:  # found, but the hook never reported it written
        assert ENGLISH in (await client.post("/api/notices/pending", json=_start("cc", "s1"))).json()["user_text"]
    assert await _status(db_url, "feedback_intro:cc") == "active"
    monkeypatch.setenv("AITEAM_TOOLSETS", "default")
    await _session_start(db_url, "cc", "s2")
    assert await _status(db_url, "feedback_intro:cc") == "cleared"


@pytest.mark.parametrize("source", ["resume", "fork", "clear", "compact"])
async def test_only_a_fresh_session_start_says_it(db_url, source) -> None:
    async with _client(db_url) as client:
        answer = (await client.post("/api/notices/pending", json={**_start("cc", "s1"), "source": source})).json()
    assert ENGLISH not in answer["user_text"]
    assert ENGLISH in (await _session_start(db_url, "cc", "s2"))["user_text"]


def test_the_introduction_comes_after_news_of_the_same_severity() -> None:
    """The live start hook showed it in place of a release line: within its severity it yields."""
    from datetime import UTC, datetime

    from aiteam.types import Notice

    def pick(catalog_id: str, seen: datetime) -> ledger._Pick:
        notice = Notice(key=catalog_id, catalog_id=catalog_id, first_seen_at=seen)
        return ledger._Pick(notice=notice, entry=CATALOG[catalog_id])

    intro = pick("feedback_intro", datetime(2026, 1, 1, tzinfo=UTC))
    release = pick("release_available", datetime(2026, 1, 2, tzinfo=UTC))
    assert CATALOG["feedback_intro"].severity == CATALOG["release_available"].severity
    assert sorted([intro, release], key=ledger._Pick.rank) == [release, intro]
