"""Integration-style test for v1.5.0-D refresher + worker collaboration.

Wires together ``EcosystemShallowQueueWorker`` and ``EcosystemRefresher``
to exercise the cron-style flow end-to-end without subprocess:

  1. Project starts with one summarized active repo.
  2. Cron tick → refresher.shallow_refresh() probes GitHub.
  3. Repo has a newer push than ``last_shallow_refreshed_at`` → worker
     queues a Stage 0 dispatch and creates a deep_review row.
  4. Apply the agent's summary back through the repository helper to
     simulate the writeback hook; ``last_shallow_refreshed_at`` advances.
  5. Subsequent refresh with the same pushed_at is a no-op (no diff).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio

from aiteam.services.ecosystem_refresher import EcosystemRefresher
from aiteam.services.ecosystem_shallow_queue import EcosystemShallowQueueWorker
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReviewStatus,
    EcosystemProjectSettings,
    EcosystemRepoProfile,
    EcosystemStageStatus,
)

PROJECT = "proj-integration"


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _seed_basics(repo: StorageRepository) -> str:
    settings = EcosystemProjectSettings(
        project_id=PROJECT, min_stars=1000, top_n=5
    )
    await repo.upsert_ecosystem_project_settings(settings)
    profile = EcosystemRepoProfile(
        project_id=PROJECT,
        repo_full_name="owner/iter",
        name="iter",
        owner="owner",
        stars=8000,
        shallow_summary="第一版总结",
        last_shallow_refreshed_at=datetime.now(tz=UTC)
        - timedelta(days=10),
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile, project_id=PROJECT)
    fetched = await repo.get_ecosystem_profile(
        "owner/iter", project_id=PROJECT
    )
    assert fetched is not None
    return fetched.id


async def test_refresh_then_writeback_then_diff_skip(
    repo: StorageRepository,
) -> None:
    repo_id = await _seed_basics(repo)
    fresh_push = datetime.now(tz=UTC).isoformat()

    calls: list[str] = []

    async def fetcher(repo_full_name: str) -> dict[str, Any]:
        calls.append(repo_full_name)
        return {
            "http_status": 200,
            "stars": 8500,
            "pushed_at": fresh_push,
        }

    worker = EcosystemShallowQueueWorker(repo, project_id=PROJECT)
    refresher = EcosystemRefresher(
        repo, worker, gh_fetcher=fetcher, project_id=PROJECT
    )

    # First cycle: should detect the new push and queue Stage 0.
    first = await refresher.shallow_refresh()
    assert first.refreshed == 1
    assert first.snapshots_written == 1
    assert len(first.queued_intents) == 1
    intent = first.queued_intents[0]

    # Verify deep_review row created in-flight — D5: stage=queued with the
    # tick claim held; status is a derived view and stays 'queued'.
    review = await repo.get_deep_review(
        intent.deep_review_id, project_id=PROJECT
    )
    assert review is not None
    assert review.status == EcosystemDeepReviewStatus.QUEUED
    assert review.stage_status == EcosystemStageStatus.QUEUED

    # Simulate the agent writeback: update summary + advance refreshed_at.
    await repo.update_profile_shallow_summary(
        repo_id,
        shallow_summary="第二版总结：增量更新后的内容",
        project_id=PROJECT,
    )
    await repo.update_deep_review_stage(
        intent.deep_review_id,
        EcosystemStageStatus.SHALLOW_DONE,
        project_id=PROJECT,
    )

    # Second cycle: same pushed_at as before → should be skipped (no diff).
    second = await refresher.shallow_refresh()
    assert second.refreshed == 0
    assert second.skipped_no_diff == 1
    assert len(second.queued_intents) == 0
    # Snapshot table accumulates (append-only).
    snapshots = await repo.list_status_snapshots(
        repo_id=repo_id, project_id=PROJECT
    )
    assert len(snapshots) == 2
    # Each snapshot should reference its scan_run_id.
    assert snapshots[0].scan_run_id == second.scan_run_id
    assert snapshots[1].scan_run_id == first.scan_run_id

    # Fetcher called once per cycle.
    assert calls.count("owner/iter") == 2
