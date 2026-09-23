"""meeting_conclude carries the ecosystem writeback reminder in its result.

The reminder used to be printed by a PostToolUse hook on stdout, which Claude
Code keeps only in its debug log, so the model never saw it. It now rides in
the conclude result that the caller reads. Rows are created in the database and
read back through the HTTP route, so the assertions cross the persistence
boundary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiteam.api.deps import get_event_bus, get_repository
from aiteam.api.routes.meetings import router
from aiteam.types import EcosystemDeepReview, EcosystemDeepReviewStatus, EcosystemRepoProfile


def _app(repo) -> FastAPI:
    app = FastAPI()
    bus = AsyncMock()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_event_bus] = lambda: bus
    app.include_router(router)
    return app


async def _meeting(repo, topic: str) -> str:
    team = await repo.create_team(name=f"team-{abs(hash(topic))}", mode="coordinate")
    meeting = await repo.create_meeting(team_id=team.id, topic=topic, participants=[])
    return meeting.id


async def _review_linked_to(repo, meeting_id: str, full_name: str) -> str:
    owner, name = full_name.split("/")
    await repo.upsert_ecosystem_profile(
        EcosystemRepoProfile(repo_full_name=full_name, name=name, owner=owner, stars=10)
    )
    profile = await repo.get_ecosystem_profile(full_name)
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=profile.id, status=EcosystemDeepReviewStatus.QUEUED)
    )
    if meeting_id:
        await repo.update_deep_review_stage(
            review.id, "debated", debate_meeting_id=meeting_id
        )
    return review.id


async def _conclude(repo, meeting_id: str) -> dict:
    async with AsyncClient(transport=ASGITransport(app=_app(repo)), base_url="http://t") as client:
        resp = await client.put(f"/api/meetings/{meeting_id}/conclude", json={"force": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["status"] == "concluded"
    return body


@pytest.mark.asyncio
async def test_linked_reviews_are_listed_even_without_a_keyword(db_repository):
    """The link is the strong signal: a debate whose topic names no keyword still needs writeback."""
    meeting_id = await _meeting(db_repository, "adopt or skip: two finalists")
    linked = await _review_linked_to(db_repository, meeting_id, "acme/finalist-a")
    await _review_linked_to(db_repository, "", "acme/unrelated")

    hint = (await _conclude(db_repository, meeting_id))["ecosystem_writeback"]

    assert hint["review_ids"] == [linked]
    assert hint["matched_keywords"] == []
    assert "ecosystem_apply_debate_result" in hint["next_step"]
    assert "ecosystem_mark_as_reference" in hint["next_step"]


@pytest.mark.asyncio
async def test_keyword_topic_without_a_link_points_at_the_link_tool(db_repository):
    meeting_id = await _meeting(db_repository, "生态库 深扫结论复盘")

    hint = (await _conclude(db_repository, meeting_id))["ecosystem_writeback"]

    assert hint["review_ids"] == []
    assert "生态库" in hint["matched_keywords"] and "深扫" in hint["matched_keywords"]
    assert "ecosystem_link_debate_meeting" in hint["next_step"]


@pytest.mark.asyncio
async def test_plain_meeting_carries_no_hint(db_repository):
    meeting_id = await _meeting(db_repository, "weekly sync")
    await _review_linked_to(db_repository, "", "acme/elsewhere")

    body = await _conclude(db_repository, meeting_id)

    assert body["ecosystem_writeback"] is None


@pytest.mark.asyncio
async def test_a_failed_lookup_never_fails_the_conclude(db_repository, monkeypatch):
    """The meeting is already concluded when the lookup runs; losing the hint is the only cost."""
    meeting_id = await _meeting(db_repository, "ecosystem debate")

    async def boom(*_a, **_k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(db_repository, "list_deep_reviews", boom)

    body = await _conclude(db_repository, meeting_id)

    assert body["ecosystem_writeback"] is None
    assert (await db_repository.get_meeting(meeting_id)).status == "concluded"
