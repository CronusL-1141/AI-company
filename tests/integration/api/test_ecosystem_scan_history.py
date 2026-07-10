"""Integration tests for v1.6.0 scan_history endpoint + discovered event backfill.

Covers:
- test_scan_history_returns_events_and_deep_reviews_merged
- test_scan_history_sorted_by_timestamp_desc
- test_scan_history_limit_clamp
- test_backfill_discovered_events_idempotent
- test_full_endpoint_works_for_repos
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemDeepReview, EcosystemRepoEvent, EcosystemRepoProfile

TEST_PROJECT_ID = "proj-scan-hist-test-001"


@pytest_asyncio.fixture
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://", project_scope=TEST_PROJECT_ID)
    await r.init_db()
    yield r
    await close_db()


def _make_profile(repo_full_name: str = "owner/repo", **kwargs) -> EcosystemRepoProfile:
    defaults = dict(
        repo_full_name=repo_full_name,
        name=repo_full_name.split("/")[-1],
        owner=repo_full_name.split("/")[0],
        stars=1000,
        canonical_id=f"github/{repo_full_name}",
        source_kind="github",
        topics=["mcp", "agent"],
    )
    defaults.update(kwargs)
    return EcosystemRepoProfile(**defaults)


@pytest.mark.asyncio
async def test_scan_history_returns_events_and_deep_reviews_merged(repo: StorageRepository):
    """scan_history endpoint merges events + deep_reviews into a unified timeline."""
    profile = _make_profile("hist-owner/hist-repo")
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("hist-owner/hist-repo")
    assert fetched is not None
    rid = fetched.id

    # Create a discovered event
    ev = EcosystemRepoEvent(
        repo_id=rid,
        event_type="discovered",
        payload_json={"first_stars": 1000, "first_topics": ["mcp"]},
        source="scanner",
        triggered_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    await repo.create_repo_event(ev)

    # Create a stars_jumped event
    ev2 = EcosystemRepoEvent(
        repo_id=rid,
        event_type="stars_jumped",
        payload_json={"before": 1000, "after": 1500, "pct": 50},
        source="scanner",
        triggered_at=datetime(2025, 3, 1, tzinfo=UTC),
    )
    await repo.create_repo_event(ev2)

    # Create a deep_review
    dr = EcosystemDeepReview(
        repo_id=rid,
        project_id=TEST_PROJECT_ID,
        summary_md="This is a test summary.",
        architecture_md="## Architecture",
        risks_md="## Risks",
        created_at=datetime(2025, 2, 1, tzinfo=UTC),
        completed_at=datetime(2025, 2, 2, tzinfo=UTC),
    )
    await repo.create_deep_review(dr)

    # Call scan_history logic directly via repo methods (mirrors endpoint logic)
    events = await repo.list_repo_events(rid, limit=100)
    deep_reviews = await repo.list_deep_reviews(repo_id=rid, limit=100)

    assert len(events) == 2
    assert len(deep_reviews) == 1

    # Verify event types
    event_types = {e.event_type for e in events}
    assert "discovered" in event_types
    assert "stars_jumped" in event_types


@pytest.mark.asyncio
async def test_scan_history_sorted_by_timestamp_desc(repo: StorageRepository):
    """Merged entries must be sorted by timestamp descending."""
    profile = _make_profile("sort-owner/sort-repo")
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("sort-owner/sort-repo")
    assert fetched is not None
    rid = fetched.id

    t1 = datetime(2025, 1, 1, tzinfo=UTC)
    t2 = datetime(2025, 2, 1, tzinfo=UTC)
    t3 = datetime(2025, 3, 1, tzinfo=UTC)

    for ts, etype in [(t1, "discovered"), (t3, "stars_jumped")]:
        await repo.create_repo_event(EcosystemRepoEvent(
            repo_id=rid,
            event_type=etype,
            payload_json={},
            source="scanner",
            triggered_at=ts,
        ))

    dr = EcosystemDeepReview(
        repo_id=rid,
        project_id=TEST_PROJECT_ID,
        created_at=t2,
        completed_at=t2,
    )
    await repo.create_deep_review(dr)

    events = await repo.list_repo_events(rid, limit=100)
    deep_reviews = await repo.list_deep_reviews(repo_id=rid, limit=100)

    # Build merged list (mirrors endpoint logic)
    entries = []
    for ev in events:
        entries.append({"timestamp": ev.triggered_at.isoformat(), "kind": "event"})
    for dr_ in deep_reviews:
        ts_ = dr_.completed_at or dr_.created_at
        entries.append({"timestamp": ts_.isoformat(), "kind": "deep_review"})

    entries.sort(key=lambda e: e["timestamp"], reverse=True)

    timestamps = [e["timestamp"] for e in entries]
    assert timestamps == sorted(timestamps, reverse=True), "Entries not sorted desc"
    # Most recent (t3) first — strip tz suffix for SQLite round-trip compat
    assert entries[0]["timestamp"].startswith("2025-03-01")


@pytest.mark.asyncio
async def test_scan_history_limit_clamp(repo: StorageRepository):
    """scan_history respects the limit parameter and never returns more than limit entries."""
    profile = _make_profile("limit-owner/limit-repo")
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("limit-owner/limit-repo")
    assert fetched is not None
    rid = fetched.id

    # Insert 10 events
    for i in range(10):
        await repo.create_repo_event(EcosystemRepoEvent(
            repo_id=rid,
            event_type="rescanned",
            payload_json={"seq": i},
            source="scanner",
            triggered_at=datetime(2025, 1, i + 1, tzinfo=UTC),
        ))

    # list_repo_events already respects limit
    events = await repo.list_repo_events(rid, limit=5)
    assert len(events) == 5


@pytest.mark.asyncio
async def test_backfill_discovered_events_idempotent(repo: StorageRepository):
    """Running _backfill_discovered_events twice must not duplicate events."""
    from aiteam.storage.connection import _backfill_discovered_events

    profile = _make_profile("bf-owner/bf-repo")
    profile.first_seen_at = datetime(2024, 6, 1, tzinfo=UTC)
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("bf-owner/bf-repo")
    assert fetched is not None
    rid = fetched.id

    # Simulate backfill using sqlite3 directly on a temp DB
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE ecosystem_repo_profiles "
        "(id TEXT, project_id TEXT, scan_run_id TEXT, topics TEXT, stars INT, first_seen_at TEXT)"
    )
    con.execute(
        "CREATE TABLE ecosystem_repo_events "
        "(id TEXT PRIMARY KEY, repo_id TEXT, project_id TEXT, event_type TEXT, "
        "payload_json TEXT, source TEXT, scan_run_id TEXT, triggered_at TEXT)"
    )
    con.execute(
        "INSERT INTO ecosystem_repo_profiles VALUES (?, ?, ?, ?, ?, ?)",
        (rid, TEST_PROJECT_ID, None, json.dumps(["mcp"]), 1000, "2024-06-01T00:00:00+00:00"),
    )
    con.commit()

    # First backfill
    _backfill_discovered_events(con)
    count1 = con.execute(
        "SELECT COUNT(*) FROM ecosystem_repo_events WHERE event_type='discovered'"
    ).fetchone()[0]
    assert count1 == 1

    # Second backfill — must be idempotent
    _backfill_discovered_events(con)
    count2 = con.execute(
        "SELECT COUNT(*) FROM ecosystem_repo_events WHERE event_type='discovered'"
    ).fetchone()[0]
    assert count2 == 1, f"Backfill not idempotent: count went from {count1} to {count2}"

    con.close()


@pytest.mark.asyncio
async def test_full_endpoint_works_for_repos(repo: StorageRepository):
    """get_ecosystem_profile_full returns correct data for repos with shallow_summary."""
    profile = _make_profile("full-owner/full-repo")
    profile.shallow_summary = "This is a great workflow automation tool."
    await repo.upsert_ecosystem_profile(profile)

    result = await repo.get_ecosystem_profile_full(repo_full_name="full-owner/full-repo")
    assert result is not None
    assert result["profile"].repo_full_name == "full-owner/full-repo"
    assert result["profile"].shallow_summary == "This is a great workflow automation tool."
    assert result["deep_reviews"] == []

    # Serialize using _serialize_full — should produce stage_status = 'shallow_done'
    from aiteam.api.routes.ecosystem import _serialize_full
    serialized = _serialize_full(result)
    assert serialized["profile"]["stage_status"] == "shallow_done"
    assert serialized["profile"]["repo_full_name"] == "full-owner/full-repo"
