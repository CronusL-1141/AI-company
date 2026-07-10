"""生态仓深扫报告存储层单元测试 — create / get / update / list 覆盖。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    DemoResult,
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
    IntegrationRecommendation,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """内存 SQLite 仓库用于测试。"""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def sample_repo_id(repo: StorageRepository) -> str:
    """先建一个 EcosystemRepoProfile 返回其 id 用作 FK。"""
    profile = EcosystemRepoProfile(
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        stars=50000,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("anthropics/claude-code")
    assert fetched is not None
    return fetched.id


def _make_review(
    repo_id: str,
    agent_id: str | None = None,
    **extra,
) -> EcosystemDeepReview:
    # D5: status 由 create_deep_review 按 stage_status 派生，不再由调用方传入；
    # 需要特定 status 的测试改传 stage_status（见 STAGE_TO_STATUS 映射）。
    return EcosystemDeepReview(
        repo_id=repo_id,
        agent_id=agent_id,
        summary_md="# Summary",
        architecture_md="架构说明",
        risks_md="风险",
        learnings_md="经验",
        **extra,
    )


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


async def test_create_deep_review_persists(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """创建深扫报告后能按 id 取回。"""
    review = _make_review(sample_repo_id)
    await repo.create_deep_review(review)

    fetched = await repo.get_deep_review(review.id)
    assert fetched is not None
    assert fetched.repo_id == sample_repo_id
    assert fetched.status == EcosystemDeepReviewStatus.QUEUED


async def test_get_deep_review_missing_returns_none(repo: StorageRepository) -> None:
    """不存在的 id 返回 None。"""
    fetched = await repo.get_deep_review("non-existent-id")
    assert fetched is None


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_deep_review_status_transition(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """status 由 queued -> running -> completed 可逐步更新。"""
    review = _make_review(sample_repo_id)
    await repo.create_deep_review(review)

    updated = await repo.update_deep_review(
        review.id, status=EcosystemDeepReviewStatus.RUNNING
    )
    assert updated is not None
    assert updated.status == EcosystemDeepReviewStatus.RUNNING

    completed = await repo.update_deep_review(
        review.id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        completed_at=datetime.now(tz=UTC),
        duration_seconds=120.5,
        integration_recommendation=IntegrationRecommendation.INTEGRATE,
        demo_result=DemoResult.SUCCESS,
    )
    assert completed is not None
    assert completed.status == EcosystemDeepReviewStatus.COMPLETED
    assert completed.duration_seconds == 120.5
    assert completed.integration_recommendation == IntegrationRecommendation.INTEGRATE
    assert completed.demo_result == DemoResult.SUCCESS


async def test_update_deep_review_missing_returns_none(repo: StorageRepository) -> None:
    """更新不存在的 review 返回 None。"""
    result = await repo.update_deep_review(
        "missing-id", status=EcosystemDeepReviewStatus.RUNNING
    )
    assert result is None


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_deep_reviews_filter_by_repo_id(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """按 repo_id 过滤只返回该仓的报告。"""
    # 创建第二个 repo
    second_profile = EcosystemRepoProfile(
        repo_full_name="other/repo",
        name="repo",
        owner="other",
        stars=8000,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(second_profile)
    second = await repo.get_ecosystem_profile("other/repo")
    assert second is not None

    await repo.create_deep_review(_make_review(sample_repo_id))
    await repo.create_deep_review(_make_review(sample_repo_id))
    await repo.create_deep_review(_make_review(second.id))

    rows_a = await repo.list_deep_reviews(repo_id=sample_repo_id)
    rows_b = await repo.list_deep_reviews(repo_id=second.id)
    assert len(rows_a) == 2
    assert len(rows_b) == 1
    assert all(r.repo_id == sample_repo_id for r in rows_a)


async def test_list_deep_reviews_filter_by_status(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """按 status 过滤只返回符合状态的报告。

    D5: create_deep_review 强制按 stage_status 派生 status（唯一真源
    types.STAGE_TO_STATUS），因此通过 stage 构造各 status 值——
    queued→queued / shallow_done→completed / shallow_failed→failed。
    """
    from aiteam.types import EcosystemStageStatus

    await repo.create_deep_review(
        _make_review(sample_repo_id, stage_status=EcosystemStageStatus.QUEUED)
    )
    await repo.create_deep_review(
        _make_review(sample_repo_id, stage_status=EcosystemStageStatus.SHALLOW_DONE)
    )
    await repo.create_deep_review(
        _make_review(sample_repo_id, stage_status=EcosystemStageStatus.SHALLOW_FAILED)
    )

    queued = await repo.list_deep_reviews(status="queued")
    completed = await repo.list_deep_reviews(status="completed")
    assert len(queued) == 1
    assert len(completed) == 1
    assert queued[0].status == EcosystemDeepReviewStatus.QUEUED
    assert completed[0].status == EcosystemDeepReviewStatus.COMPLETED
