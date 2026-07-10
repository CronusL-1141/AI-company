"""Unit tests for v1.5.0-B Stage 0 self-learning loop.

Covers §3.2 of the v1.5.0 design: when the same fetch-style failure is
seen across >= SELF_LEARNING_THRESHOLD distinct repos, the worker emits
a ``pattern_record`` failure entry so future Stage 0 prompts can inject
the lesson.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest_asyncio

from aiteam.services.ecosystem_shallow_queue import (
    SELF_LEARNING_THRESHOLD,
    EcosystemShallowQueueWorker,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoProfile


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _seed_profile(
    repo: StorageRepository,
    full_name: str,
    *,
    project_id: str = "proj-learn",
) -> str:
    profile = EcosystemRepoProfile(
        project_id=project_id,
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=2000,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile, project_id=project_id)
    fetched = await repo.get_ecosystem_profile(full_name, project_id=project_id)
    assert fetched is not None
    return fetched.id


class _FakePatternRecorder:
    """Capturing recorder used to assert pattern_record was triggered."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"mem-{len(self.calls)}"


# ============================================================
# Trigger threshold
# ============================================================


async def test_does_not_record_below_threshold(repo: StorageRepository) -> None:
    """Below threshold the worker silently bookkeeps without recording."""
    rec = _FakePatternRecorder()
    worker = EcosystemShallowQueueWorker(
        repo, project_id="proj-learn", pattern_recorder=rec
    )
    rid_a = await _seed_profile(repo, "owner/a")
    rid_b = await _seed_profile(repo, "owner/b")

    await worker.report_failure(
        rid_a, error_kind="fetch_style", error_message="too short"
    )
    await worker.report_failure(
        rid_b, error_kind="fetch_style", error_message="too short"
    )
    assert rec.calls == []


async def test_records_pattern_at_threshold(repo: StorageRepository) -> None:
    """Hitting the threshold emits exactly one pattern_record call."""
    rec = _FakePatternRecorder()
    worker = EcosystemShallowQueueWorker(
        repo, project_id="proj-learn", pattern_recorder=rec
    )

    for i in range(SELF_LEARNING_THRESHOLD):
        rid = await _seed_profile(repo, f"owner/r{i}")
        await worker.report_failure(
            rid,
            error_kind="fetch_style",
            error_message="description too short",
        )

    assert len(rec.calls) == 1
    payload = rec.calls[0]
    assert payload["task_type"] == "ecosystem-shallow-fetch"
    assert payload["agent_template"] == "ai-engineer"
    assert "fetch_style" in payload["approach"]
    assert "lesson" in payload
    assert payload["lesson"]


async def test_does_not_double_record_after_threshold(
    repo: StorageRepository,
) -> None:
    """After firing once the bucket resets so we don't spam memory."""
    rec = _FakePatternRecorder()
    worker = EcosystemShallowQueueWorker(
        repo, project_id="proj-learn", pattern_recorder=rec
    )

    # First 3 repos -> records once
    for i in range(SELF_LEARNING_THRESHOLD):
        rid = await _seed_profile(repo, f"owner/first{i}")
        await worker.report_failure(
            rid, error_kind="fetch_style", error_message="too short"
        )
    assert len(rec.calls) == 1

    # Two more failures from same class — should NOT re-record yet
    for i in range(SELF_LEARNING_THRESHOLD - 1):
        rid = await _seed_profile(repo, f"owner/second{i}")
        await worker.report_failure(
            rid, error_kind="fetch_style", error_message="too short"
        )
    assert len(rec.calls) == 1

    # One more brings the bucket back to threshold — records again
    rid = await _seed_profile(repo, "owner/third")
    await worker.report_failure(
        rid, error_kind="fetch_style", error_message="too short"
    )
    assert len(rec.calls) == 2


async def test_non_eligible_failures_never_record(
    repo: StorageRepository,
) -> None:
    """Only ``learning_eligible`` decisions feed into the recorder."""
    rec = _FakePatternRecorder()
    worker = EcosystemShallowQueueWorker(
        repo, project_id="proj-learn", pattern_recorder=rec
    )

    # Spam HTTP 500 errors — transient class, not learning-eligible.
    for i in range(10):
        rid = await _seed_profile(repo, f"owner/r{i}")
        await worker.report_failure(
            rid, error_kind="http", http_status=500
        )

    assert rec.calls == []


async def test_distinct_repos_required(repo: StorageRepository) -> None:
    """Same repo failing repeatedly counts once toward the threshold."""
    rec = _FakePatternRecorder()
    worker = EcosystemShallowQueueWorker(
        repo, project_id="proj-learn", pattern_recorder=rec
    )

    rid = await _seed_profile(repo, "owner/single")
    for _ in range(5 * SELF_LEARNING_THRESHOLD):
        await worker.report_failure(
            rid, error_kind="fetch_style", error_message="x"
        )

    # Single repo never crosses the threshold (set has size 1)
    assert rec.calls == []
