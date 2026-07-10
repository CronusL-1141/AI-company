"""EcosystemDeepReviewer service unit tests.

Covers the lifecycle: request -> running -> link_report -> completed.
Watchdog timeout path uses a tiny timeout to keep the suite fast.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.services.ecosystem_deep_reviewer import (
    DEEP_REVIEW_AGENT_PROMPT,
    EcosystemDeepReviewer,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
    EcosystemStageStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite-backed StorageRepository."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def repo_id(repo: StorageRepository) -> str:
    profile = EcosystemRepoProfile(
        repo_full_name="prefecthq/fastmcp",
        name="fastmcp",
        owner="prefecthq",
        stars=25000,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("prefecthq/fastmcp")
    assert fetched is not None
    return fetched.id


# ---------------------------------------------------------------------------
# request
# ---------------------------------------------------------------------------


async def test_request_creates_queued_review_with_prompt(
    repo: StorageRepository, repo_id: str
) -> None:
    """request() creates an in-flight row (stage=queued, claim held) with prompt.

    D5: status is a derived read-only view — no more RUNNING writes; the
    in-flight semantics live on stage_status=queued + claimed_by.
    """
    reviewer = EcosystemDeepReviewer(repo)

    review = await reviewer.request(
        repo_id=repo_id, priority="medium", timeout_minutes=45
    )

    assert review.status == EcosystemDeepReviewStatus.QUEUED
    assert review.stage_status == EcosystemStageStatus.QUEUED
    assert review.claimed_by == "deep-reviewer:manual"
    assert review.claimed_at is not None
    assert review.started_at is not None
    assert review.repo_id == repo_id
    # K5: dispatch prompt now stored in dedicated dispatch_prompt column,
    # NOT demo_log_excerpt (which is reserved for actual demo output).
    assert "prefecthq/fastmcp" in review.dispatch_prompt
    assert review.id in review.dispatch_prompt
    assert repo_id in review.dispatch_prompt
    # Prompt structure references the 5-section template.
    assert (
        "5 sections" in review.dispatch_prompt
        or "5-section" in review.dispatch_prompt.lower()
    )
    # demo_log_excerpt must remain empty until the actual report is linked.
    assert review.demo_log_excerpt == ""


async def test_request_unknown_repo_raises(repo: StorageRepository) -> None:
    """Requesting against an unknown repo_id raises ValueError."""
    reviewer = EcosystemDeepReviewer(repo)
    with pytest.raises(ValueError):
        await reviewer.request(repo_id="missing-repo")


async def test_request_rejects_concurrent_review(
    repo: StorageRepository, repo_id: str
) -> None:
    """Cannot queue a second deep-review for a repo while one is in-flight."""
    reviewer = EcosystemDeepReviewer(repo)
    await reviewer.request(repo_id=repo_id, timeout_minutes=45)

    with pytest.raises(ValueError):
        await reviewer.request(repo_id=repo_id, timeout_minutes=45)


# ---------------------------------------------------------------------------
# status / list
# ---------------------------------------------------------------------------


async def test_status_returns_latest_review(
    repo: StorageRepository, repo_id: str
) -> None:
    """status() returns the most recent review for a repo."""
    reviewer = EcosystemDeepReviewer(repo)
    review = await reviewer.request(repo_id=repo_id, timeout_minutes=45)

    fetched = await reviewer.status(repo_id=repo_id)
    assert fetched is not None
    assert fetched.id == review.id


async def test_list_filters_by_status(
    repo: StorageRepository, repo_id: str
) -> None:
    reviewer = EcosystemDeepReviewer(repo)
    review = await reviewer.request(repo_id=repo_id, timeout_minutes=45)

    # D5: new in-flight rows are status='queued' (derived); 'running' only
    # ever matches pre-v1.6.2 historical rows.
    queued_rows = await reviewer.list_reviews(status="queued")
    assert len(queued_rows) == 1
    assert queued_rows[0].id == review.id

    running_rows = await reviewer.list_reviews(status="running")
    assert running_rows == []


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_cancel_marks_failed(
    repo: StorageRepository, repo_id: str
) -> None:
    """cancel() flips status to failed and records a cancellation note."""
    reviewer = EcosystemDeepReviewer(repo)
    review = await reviewer.request(repo_id=repo_id, timeout_minutes=45)

    cancelled = await reviewer.cancel(review.id)
    assert cancelled is not None
    assert cancelled.status == EcosystemDeepReviewStatus.FAILED
    assert "cancelled" in cancelled.risks_md.lower()


async def test_cancel_unknown_returns_none(repo: StorageRepository) -> None:
    reviewer = EcosystemDeepReviewer(repo)
    assert await reviewer.cancel("missing-id") is None


# ---------------------------------------------------------------------------
# link_report
# ---------------------------------------------------------------------------


async def test_link_report_completes_the_review(
    repo: StorageRepository, repo_id: str
) -> None:
    """link_report() wires report_id and flips status to completed."""
    reviewer = EcosystemDeepReviewer(repo)
    review = await reviewer.request(repo_id=repo_id, timeout_minutes=45)

    linked = await reviewer.link_report(review.id, "report-uuid-123")
    assert linked is not None
    assert linked.status == EcosystemDeepReviewStatus.COMPLETED
    assert linked.report_id == "report-uuid-123"
    assert linked.completed_at is not None


async def test_link_report_is_idempotent(
    repo: StorageRepository, repo_id: str
) -> None:
    """A second link_report call leaves the existing report_id alone."""
    reviewer = EcosystemDeepReviewer(repo)
    review = await reviewer.request(repo_id=repo_id, timeout_minutes=45)

    first = await reviewer.link_report(review.id, "first-id")
    assert first is not None and first.report_id == "first-id"

    second = await reviewer.link_report(review.id, "second-id")
    assert second is not None
    assert second.report_id == "first-id"  # unchanged


# ---------------------------------------------------------------------------
# Watchdog timeout
# ---------------------------------------------------------------------------


async def test_watchdog_marks_failed_after_timeout(
    repo: StorageRepository, repo_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review still running past its timeout is force-failed by the watchdog."""
    reviewer = EcosystemDeepReviewer(repo)

    # Override the spawned watchdog to use seconds rather than minutes for speed.
    real_spawn = reviewer._spawn_watchdog

    def short_spawn(deep_review_id: str, _timeout_seconds: float) -> None:
        real_spawn(deep_review_id, 0.05)

    monkeypatch.setattr(reviewer, "_spawn_watchdog", short_spawn)

    review = await reviewer.request(repo_id=repo_id, timeout_minutes=45)

    # Wait long enough for the watchdog to fire and the DB write to land.
    await asyncio.sleep(0.3)

    fetched = await repo.get_deep_review(review.id)
    assert fetched is not None
    assert fetched.status == EcosystemDeepReviewStatus.FAILED
    assert "timeout" in fetched.risks_md.lower()


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------


def test_prompt_template_has_all_anchors() -> None:
    """The dispatch prompt references repo_id / deep_review_id / timeout."""
    rendered = DEEP_REVIEW_AGENT_PROMPT.format(
        repo_full_name="anthropics/claude-code",
        repo_full_name_slug="anthropics-claude-code",
        repo_id="repo-uuid",
        deep_review_id="dr-uuid",
        timeout_minutes=45,
    )
    assert "repo_id=repo-uuid" in rendered
    assert "deep_review_id=dr-uuid" in rendered
    assert "anthropics/claude-code" in rendered
    assert "report_save" in rendered
    assert "5 section" in rendered.lower()
