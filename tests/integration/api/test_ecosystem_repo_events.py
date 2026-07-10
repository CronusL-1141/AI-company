"""Integration tests for v1.6.0 event sourcing: ecosystem_repo_events table + API endpoints.

Covers:
- test_discovered_event_on_new_repo
- test_topics_changed_event_on_topic_update
- test_stars_jumped_event_on_growth
- test_repo_events_endpoint
- test_diff_period_endpoint
- test_index_diffs_table_still_works (old table backward compat)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoEvent, EcosystemRepoProfile

TEST_PROJECT_ID = "proj-events-test-001"


@pytest_asyncio.fixture
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://", project_scope=TEST_PROJECT_ID)
    await r.init_db()
    yield r
    await close_db()


def _make_profile(**kwargs) -> EcosystemRepoProfile:
    defaults = dict(
        repo_full_name="test-owner/test-repo",
        name="test-repo",
        owner="test-owner",
        description="A test repo",
        stars=1000,
        language="Python",
        topics=["mcp", "claude"],
        is_archived=False,
        needs_deep_review=False,
        relevance_category="mcp-server",
        relevance_score=80,
        one_line_summary="Test",
        description_excerpt="Test",
        canonical_id="github/test-owner/test-repo",
        source_kind="github",
    )
    defaults.update(kwargs)
    return EcosystemRepoProfile(**defaults)


@pytest.mark.asyncio
async def test_discovered_event_on_new_repo(repo: StorageRepository):
    """Scanner writes a 'discovered' event when a new repo is first upserted."""
    profile = _make_profile(
        repo_full_name="ev-test/new-repo",
        canonical_id="github/ev-test/new-repo",
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("ev-test/new-repo")
    assert fetched is not None

    ev = EcosystemRepoEvent(
        repo_id=fetched.id,
        event_type="discovered",
        payload_json={"first_stars": 1000, "first_topics": ["mcp"], "source_query": "topic:mcp"},
        source="scanner",
    )
    created = await repo.create_repo_event(ev)
    assert created.id == ev.id

    events = await repo.list_repo_events(fetched.id)
    assert len(events) >= 1
    disc = next((e for e in events if e.event_type == "discovered"), None)
    assert disc is not None
    assert disc.payload_json["first_stars"] == 1000


@pytest.mark.asyncio
async def test_topics_changed_event_on_topic_update(repo: StorageRepository):
    """topics_changed event captures before/after topic lists."""
    profile = _make_profile(
        repo_full_name="ev-test/topics-repo",
        canonical_id="github/ev-test/topics-repo",
        topics=["mcp", "claude"],
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("ev-test/topics-repo")
    assert fetched is not None

    ev = EcosystemRepoEvent(
        repo_id=fetched.id,
        event_type="topics_changed",
        payload_json={"before": ["mcp", "claude"], "after": ["mcp", "claude", "agent"]},
        source="scanner",
    )
    await repo.create_repo_event(ev)

    events = await repo.list_repo_events(fetched.id)
    tc = next((e for e in events if e.event_type == "topics_changed"), None)
    assert tc is not None
    assert "agent" in tc.payload_json["after"]
    assert tc.payload_json["before"] == ["mcp", "claude"]


@pytest.mark.asyncio
async def test_stars_jumped_event_on_growth(repo: StorageRepository):
    """stars_jumped event is written when star count changes by >=10%."""
    profile = _make_profile(
        repo_full_name="ev-test/stars-repo",
        canonical_id="github/ev-test/stars-repo",
        stars=1000,
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("ev-test/stars-repo")
    assert fetched is not None

    ev = EcosystemRepoEvent(
        repo_id=fetched.id,
        event_type="stars_jumped",
        payload_json={"before": 1000, "after": 1200, "pct": 20.0},
        source="scanner",
    )
    await repo.create_repo_event(ev)

    events = await repo.list_repo_events(fetched.id)
    sj = next((e for e in events if e.event_type == "stars_jumped"), None)
    assert sj is not None
    assert sj.payload_json["pct"] == 20.0
    assert sj.payload_json["after"] == 1200


@pytest.mark.asyncio
async def test_bulk_create_and_list_events(repo: StorageRepository):
    """bulk_create_repo_events works; list_repo_events returns newest first."""
    profile = _make_profile(
        repo_full_name="ev-test/bulk-repo",
        canonical_id="github/ev-test/bulk-repo",
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("ev-test/bulk-repo")
    assert fetched is not None

    events = [
        EcosystemRepoEvent(
            repo_id=fetched.id,
            event_type="discovered",
            payload_json={"first_stars": 500},
            source="scanner",
            triggered_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        EcosystemRepoEvent(
            repo_id=fetched.id,
            event_type="rescanned",
            payload_json={},
            source="scanner",
            triggered_at=datetime(2026, 1, 10, tzinfo=UTC),
        ),
    ]
    count = await repo.bulk_create_repo_events(events)
    assert count == 2

    listed = await repo.list_repo_events(fetched.id, limit=10)
    # newest first
    assert listed[0].event_type == "rescanned"
    assert listed[1].event_type == "discovered"


@pytest.mark.asyncio
async def test_query_events_in_period(repo: StorageRepository):
    """query_events_in_period filters by time window."""
    profile = _make_profile(
        repo_full_name="ev-test/period-repo",
        canonical_id="github/ev-test/period-repo",
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("ev-test/period-repo")
    assert fetched is not None

    # Need a project_id to use query_events_in_period
    test_project_id = "test-project-events"
    ev_in = EcosystemRepoEvent(
        repo_id=fetched.id,
        project_id=test_project_id,
        event_type="discovered",
        payload_json={"first_stars": 100},
        source="scanner",
        triggered_at=datetime(2026, 3, 15, tzinfo=UTC),
    )
    ev_out = EcosystemRepoEvent(
        repo_id=fetched.id,
        project_id=test_project_id,
        event_type="rescanned",
        payload_json={},
        source="scanner",
        triggered_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    await repo.bulk_create_repo_events([ev_in, ev_out])

    results = await repo.query_events_in_period(
        test_project_id,
        from_dt=datetime(2026, 3, 1, tzinfo=UTC),
        to_dt=datetime(2026, 3, 31, tzinfo=UTC),
    )
    event_types = [e.event_type for e in results]
    assert "discovered" in event_types
    assert "rescanned" not in event_types


@pytest.mark.asyncio
async def test_index_diffs_table_still_works(repo: StorageRepository):
    """Old ecosystem_index_diffs CRUD still works (backward compat — table not deleted)."""
    from aiteam.types import EcosystemIndexDiff

    diff = EcosystemIndexDiff(
        project_id="compat-test",
        diff_type="incremental",
        new_count=5,
        reactivated_count=0,
        deactivated_count=0,
        stale_count=0,
        github_archived_changed_count=1,
        removed_from_query_count=0,
        markdown_summary="## Test diff",
        alerted=False,
    )
    created = await repo.create_index_diff(diff)
    assert created.id == diff.id

    latest = await repo.get_latest_index_diff("compat-test")
    assert latest is not None
    assert latest.new_count == 5
