"""Unit tests for v1.5.0-B Stage 0 failure classification & profile reactions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.services.ecosystem_shallow_queue import (
    FAILURE_AGENT_READ,
    FAILURE_AGENT_TIMEOUT,
    FAILURE_DELETED,
    FAILURE_FETCH_STYLE,
    FAILURE_JSON_PARSE,
    FAILURE_PRIVATE,
    FAILURE_RATE_LIMIT,
    FAILURE_TRANSIENT,
    MAX_RETRY_BUDGET,
    EcosystemShallowQueueWorker,
    classify_failure,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemRepoProfile,
    EcosystemStageStatus,
)

# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _seed_profile(
    repo: StorageRepository,
    full_name: str = "owner/x",
    *,
    project_id: str = "proj-test",
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


async def _seed_review(
    repo: StorageRepository,
    repo_id: str,
    project_id: str = "proj-test",
) -> str:
    review = EcosystemDeepReview(project_id=project_id, repo_id=repo_id)
    await repo.create_deep_review(review, project_id=project_id)
    return review.id


# ============================================================
# Pure classifier tests
# ============================================================


def test_classify_404_marks_deleted_no_retry() -> None:
    decision = classify_failure(error_kind="http", http_status=404)
    assert decision.failure_class == FAILURE_DELETED
    assert decision.mark_deleted is True
    assert decision.immediate_retry is False


def test_classify_403_zero_remaining_is_rate_limit() -> None:
    decision = classify_failure(
        error_kind="http", http_status=403, rate_limit_remaining=0
    )
    assert decision.failure_class == FAILURE_RATE_LIMIT
    assert decision.immediate_retry is True
    assert decision.mark_private is False


def test_classify_403_with_remaining_is_private() -> None:
    decision = classify_failure(
        error_kind="http", http_status=403, rate_limit_remaining=4500
    )
    assert decision.failure_class == FAILURE_PRIVATE
    assert decision.mark_private is True
    assert decision.immediate_retry is False


def test_classify_5xx_is_transient_immediate_retry() -> None:
    decision = classify_failure(error_kind="http", http_status=502)
    assert decision.failure_class == FAILURE_TRANSIENT
    assert decision.immediate_retry is True


def test_classify_agent_read_immediate_retry() -> None:
    decision = classify_failure(error_kind="agent_read", error_message="EOF")
    assert decision.failure_class == FAILURE_AGENT_READ
    assert decision.immediate_retry is True
    assert decision.note == "EOF"


def test_classify_first_timeout_retries_second_does_not() -> None:
    first = classify_failure(error_kind="agent_timeout", consecutive_timeouts=0)
    assert first.failure_class == FAILURE_AGENT_TIMEOUT
    assert first.immediate_retry is True

    second = classify_failure(error_kind="agent_timeout", consecutive_timeouts=1)
    assert second.failure_class == FAILURE_AGENT_TIMEOUT
    assert second.immediate_retry is False


def test_classify_json_parse_retries() -> None:
    decision = classify_failure(error_kind="json_parse")
    assert decision.failure_class == FAILURE_JSON_PARSE
    assert decision.immediate_retry is True


def test_classify_fetch_style_is_learning_eligible() -> None:
    decision = classify_failure(
        error_kind="fetch_style",
        error_message="description too short",
    )
    assert decision.failure_class == FAILURE_FETCH_STYLE
    assert decision.learning_eligible is True
    assert decision.immediate_retry is False


def test_classify_unknown_kind_falls_back_to_transient() -> None:
    decision = classify_failure(error_kind="unknown")
    assert decision.failure_class == FAILURE_TRANSIENT
    assert decision.immediate_retry is True


# ============================================================
# Worker.report_failure integration with profile flags
# ============================================================


async def test_report_failure_404_marks_profile_deleted(
    repo: StorageRepository,
) -> None:
    rid = await _seed_profile(repo, "owner/dead")
    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")

    decision = await worker.report_failure(
        rid, error_kind="http", http_status=404
    )
    assert decision.failure_class == FAILURE_DELETED

    profile = await repo.get_ecosystem_profile_by_id(
        rid, project_id="proj-test"
    )
    assert profile is not None
    assert profile.is_deleted is True
    assert profile.is_active is False


async def test_report_failure_403_private_marks_profile_private(
    repo: StorageRepository,
) -> None:
    rid = await _seed_profile(repo, "owner/secret")
    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")

    await worker.report_failure(
        rid, error_kind="http", http_status=403, rate_limit_remaining=4000
    )

    profile = await repo.get_ecosystem_profile_by_id(
        rid, project_id="proj-test"
    )
    assert profile is not None
    assert profile.is_private_now is True
    assert profile.is_active is False


async def test_report_failure_increments_count_for_transient(
    repo: StorageRepository,
) -> None:
    rid = await _seed_profile(repo, "owner/temp")
    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")

    await worker.report_failure(rid, error_kind="http", http_status=500)
    profile = await repo.get_ecosystem_profile_by_id(
        rid, project_id="proj-test"
    )
    assert profile.fetch_failure_count == 1
    assert profile.is_deleted is False
    assert profile.is_private_now is False


async def test_report_failure_escalates_to_shallow_failed_after_budget(
    repo: StorageRepository,
) -> None:
    rid = await _seed_profile(repo, "owner/repeat")
    review_id = await _seed_review(repo, rid)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")

    for _ in range(MAX_RETRY_BUDGET):
        await worker.report_failure(
            rid,
            error_kind="http",
            http_status=500,
            deep_review_id=review_id,
        )

    review = await repo.get_deep_review(review_id, project_id="proj-test")
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.SHALLOW_FAILED
