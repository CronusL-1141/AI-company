"""并发 claim 竞争条件单元测试 — worker pool 基础设施 (v1.5.3)。

验证多 worker 同时 claim 时：
- 每行最多只被一个 worker 认领（claimed_by 唯一）
- 空队列返回 None
- release 后可重新认领
- apply_quality_review 写回所有字段
- min_stars 过滤正常工作
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemRepoProfile,
    EcosystemStageStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repo for isolation."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _make_profile(repo: StorageRepository, full_name: str, stars: int = 1000) -> str:
    """Create a repo profile and return its id."""
    profile = EcosystemRepoProfile(
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=stars,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile(full_name)
    assert fetched is not None
    return fetched.id


async def _make_queued_review(repo: StorageRepository, repo_id: str) -> EcosystemDeepReview:
    """Create a queued deep review row."""
    review = EcosystemDeepReview(
        repo_id=repo_id,
        stage_status=EcosystemStageStatus.QUEUED,
    )
    return await repo.create_deep_review(review)


async def _make_shallow_done_review(repo: StorageRepository, repo_id: str) -> EcosystemDeepReview:
    """Create a shallow_done deep review row."""
    review = EcosystemDeepReview(
        repo_id=repo_id,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,
    )
    return await repo.create_deep_review(review)


# ---------------------------------------------------------------------------
# Core concurrency: claim_next_shallow_repo
# ---------------------------------------------------------------------------


async def test_concurrent_claim_shallow_no_double_claim(repo: StorageRepository) -> None:
    """10 workers 争抢 5 个 queued row：恰好 5 个成功，5 个返回 None，无重复认领。"""
    repo_ids = []
    for i in range(5):
        rid = await _make_profile(repo, f"owner/repo-shallow-{i}")
        repo_ids.append(rid)
        await _make_queued_review(repo, rid)

    async def do_claim(worker_idx: int) -> EcosystemDeepReview | None:
        return await repo.claim_next_shallow_repo(worker_id=f"worker-{worker_idx}")

    results = await asyncio.gather(*[do_claim(i) for i in range(10)])

    claimed = [r for r in results if r is not None]
    empty = [r for r in results if r is None]

    assert len(claimed) == 5, f"Expected 5 successful claims, got {len(claimed)}"
    assert len(empty) == 5, f"Expected 5 empty results, got {len(empty)}"

    # All claimed reviews must have unique claimed_by values
    claimed_by_vals = [r.claimed_by for r in claimed]
    assert len(set(claimed_by_vals)) == 5, f"Duplicate claimed_by: {claimed_by_vals}"

    # All claimed reviews must have unique ids
    claimed_ids = [r.id for r in claimed]
    assert len(set(claimed_ids)) == 5, f"Duplicate review ids: {claimed_ids}"


async def test_claim_shallow_empty_queue_returns_none(repo: StorageRepository) -> None:
    """空队列时 claim 返回 None。"""
    result = await repo.claim_next_shallow_repo(worker_id="lonely-worker")
    assert result is None


async def test_claim_shallow_already_claimed_skipped(repo: StorageRepository) -> None:
    """已被认领的行不能被第二个 worker 取走。"""
    rid = await _make_profile(repo, "owner/single-repo")
    await _make_queued_review(repo, rid)

    first = await repo.claim_next_shallow_repo(worker_id="worker-a")
    assert first is not None
    assert first.claimed_by == "worker-a"

    second = await repo.claim_next_shallow_repo(worker_id="worker-b")
    assert second is None


# ---------------------------------------------------------------------------
# release → re-claim
# ---------------------------------------------------------------------------


async def test_release_and_reclaim(repo: StorageRepository) -> None:
    """release 后，同一行可被新 worker 重新认领。"""
    rid = await _make_profile(repo, "owner/releasable-repo")
    review = await _make_queued_review(repo, rid)

    claimed = await repo.claim_next_shallow_repo(worker_id="worker-first")
    assert claimed is not None
    assert claimed.claimed_by == "worker-first"

    released = await repo.release_claim(dr_id=review.id, reason="timeout test")
    assert released is not None
    assert released.claimed_by is None
    assert released.quality_notes == "timeout test"

    reclaimed = await repo.claim_next_shallow_repo(worker_id="worker-second")
    assert reclaimed is not None
    assert reclaimed.claimed_by == "worker-second"
    assert reclaimed.id == review.id


# ---------------------------------------------------------------------------
# apply_quality_review
# ---------------------------------------------------------------------------


async def test_apply_quality_review_writes_all_fields(repo: StorageRepository) -> None:
    """apply_quality_review 写回所有 6 个字段并释放认领锁。"""
    rid = await _make_profile(repo, "owner/quality-repo")
    _ = await _make_shallow_done_review(repo, rid)
    # First claim it
    claimed = await repo.claim_next_review_repo(worker_id="qworker-1")
    assert claimed is not None
    dr, _profile = claimed

    # Apply quality review
    updated = await repo.apply_quality_review(
        dr_id=dr.id,
        quality_score=85,
        quality_notes="Good README and clear architecture",
        recommendation="integrate",
    )
    assert updated is not None
    assert updated.quality_score == 85
    assert updated.quality_notes == "Good README and clear architecture"
    assert updated.reviewed_by == "qworker-1"
    assert updated.reviewed_at is not None
    assert updated.claimed_by is None  # lock released
    assert updated.integration_recommendation is not None


async def test_apply_quality_review_missing_row_returns_none(repo: StorageRepository) -> None:
    """不存在的 dr_id 返回 None。"""
    result = await repo.apply_quality_review(
        dr_id="non-existent",
        quality_score=50,
        quality_notes="whatever",
        recommendation="skip",
    )
    assert result is None


# ---------------------------------------------------------------------------
# claim_next_review_repo + min_stars filter
# ---------------------------------------------------------------------------


async def test_claim_review_empty_queue_returns_none(repo: StorageRepository) -> None:
    """无 shallow_done 行时返回 None。"""
    result = await repo.claim_next_review_repo(worker_id="qworker-x")
    assert result is None


async def test_claim_review_returns_profile_with_summary(repo: StorageRepository) -> None:
    """认领 review 时同时返回 profile（含 shallow_summary）。"""
    rid = await _make_profile(repo, "owner/with-summary")
    profile_obj = await repo.get_ecosystem_profile_by_id(rid)
    assert profile_obj is not None

    _ = await _make_shallow_done_review(repo, rid)
    result = await repo.claim_next_review_repo(worker_id="qworker-sum")
    assert result is not None
    dr, profile = result
    assert dr.claimed_by == "qworker-sum"
    assert dr.claimed_at is not None
    assert profile.id == rid


async def test_min_stars_filter_excludes_low_star_repo(repo: StorageRepository) -> None:
    """min_stars 过滤：低星数 repo 的 review 行不被 claim。"""
    low_rid = await _make_profile(repo, "owner/low-stars-repo", stars=10)
    await _make_shallow_done_review(repo, low_rid)

    result = await repo.claim_next_review_repo(worker_id="qworker-stars", min_stars=1000)
    assert result is None


async def test_min_stars_filter_includes_high_star_repo(repo: StorageRepository) -> None:
    """min_stars 过滤：高星数 repo 的 review 行正常被 claim。"""
    high_rid = await _make_profile(repo, "owner/high-stars-repo", stars=5000)
    await _make_shallow_done_review(repo, high_rid)

    result = await repo.claim_next_review_repo(worker_id="qworker-high", min_stars=1000)
    assert result is not None
    dr, profile = result
    assert profile.stars >= 1000


# ---------------------------------------------------------------------------
# release_claim
# ---------------------------------------------------------------------------


async def test_release_claim_missing_row_returns_none(repo: StorageRepository) -> None:
    """不存在的 dr_id 释放返回 None。"""
    result = await repo.release_claim(dr_id="ghost-id", reason="does not exist")
    assert result is None


async def test_release_claim_clears_claimed_by(repo: StorageRepository) -> None:
    """release_claim 正确清空 claimed_by 并记录 reason。"""
    rid = await _make_profile(repo, "owner/to-release")
    review = await _make_queued_review(repo, rid)

    await repo.claim_next_shallow_repo(worker_id="worker-rel")
    released = await repo.release_claim(dr_id=review.id, reason="worker crashed")
    assert released is not None
    assert released.claimed_by is None
    assert released.claimed_at is None
    assert released.quality_notes == "worker crashed"
