"""v1.5.0-A: DeepReview stage_status helpers 单元测试。

覆盖：
- update_deep_review_stage 推进 + 自动写时间戳
- list_deep_reviews_by_stage 过滤
- debate_meeting_id / integration_task_id / integration_md 写入
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
    EcosystemStageStatus,
)


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """内存 SQLite 仓库用于测试。"""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def sample_repo_id(repo: StorageRepository) -> str:
    """先建一个 EcosystemRepoProfile 返回其 id。"""
    profile = EcosystemRepoProfile(
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        stars=50000,
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("anthropics/claude-code")
    assert fetched is not None
    return fetched.id


@pytest.mark.asyncio
async def test_update_deep_review_stage_promotes_shallow(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """SHALLOW_DONE 应写 shallow_completed_at 时间戳。"""
    review = await repo.create_deep_review(
        EcosystemDeepReview(
            repo_id=sample_repo_id,
            status=EcosystemDeepReviewStatus.QUEUED,
        )
    )
    assert review.stage_status == EcosystemStageStatus.QUEUED
    assert review.shallow_completed_at is None

    updated = await repo.update_deep_review_stage(
        review.id, EcosystemStageStatus.SHALLOW_DONE
    )
    assert updated is not None
    assert updated.stage_status == EcosystemStageStatus.SHALLOW_DONE
    assert updated.shallow_completed_at is not None
    assert updated.architecture_completed_at is None  # 不应写其他阶段时间戳


@pytest.mark.asyncio
async def test_update_deep_review_stage_promotes_architecture(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """ARCHITECTURE_DONE 应写 architecture_completed_at。"""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(
        review.id, EcosystemStageStatus.ARCHITECTURE_DONE
    )
    assert updated.stage_status == EcosystemStageStatus.ARCHITECTURE_DONE
    assert updated.architecture_completed_at is not None
    assert updated.shallow_completed_at is None


@pytest.mark.asyncio
async def test_update_deep_review_stage_with_meeting_id(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """DEBATED 应写 debated_at + debate_meeting_id。"""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(
        review.id,
        EcosystemStageStatus.DEBATED,
        debate_meeting_id="mtg-001",
    )
    assert updated.stage_status == EcosystemStageStatus.DEBATED
    assert updated.debated_at is not None
    assert updated.debate_meeting_id == "mtg-001"


@pytest.mark.asyncio
async def test_update_deep_review_stage_integration(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """INTEGRATED 应写 stage3_completed_at + integration_task_id + integration_md。"""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(
        review.id,
        EcosystemStageStatus.INTEGRATED,
        integration_task_id="task-int-1",
        integration_md="## 集成方案\n\n步骤一：...",
    )
    assert updated.stage_status == EcosystemStageStatus.INTEGRATED
    assert updated.stage3_completed_at is not None
    assert updated.integration_task_id == "task-int-1"
    assert updated.integration_md.startswith("## 集成方案")


@pytest.mark.asyncio
async def test_update_deep_review_stage_referenced(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """REFERENCED 也应写 stage3_completed_at。"""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(
        review.id, EcosystemStageStatus.REFERENCED
    )
    assert updated.stage_status == EcosystemStageStatus.REFERENCED
    assert updated.stage3_completed_at is not None


@pytest.mark.asyncio
async def test_update_deep_review_stage_accepts_string(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """传入字符串也能正常推进（API 边界场景）。"""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    updated = await repo.update_deep_review_stage(review.id, "shallow_done")
    assert updated.stage_status == EcosystemStageStatus.SHALLOW_DONE


@pytest.mark.asyncio
async def test_list_deep_reviews_by_stage_filters(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """list_deep_reviews_by_stage 应只返回匹配 stage 的行。"""
    r1 = await repo.create_deep_review(EcosystemDeepReview(repo_id=sample_repo_id))
    r2 = await repo.create_deep_review(EcosystemDeepReview(repo_id=sample_repo_id))
    r3 = await repo.create_deep_review(EcosystemDeepReview(repo_id=sample_repo_id))

    await repo.update_deep_review_stage(r1.id, EcosystemStageStatus.SHALLOW_DONE)
    await repo.update_deep_review_stage(r2.id, EcosystemStageStatus.DEBATED)
    # r3 keeps QUEUED

    shallow = await repo.list_deep_reviews_by_stage(EcosystemStageStatus.SHALLOW_DONE)
    debated = await repo.list_deep_reviews_by_stage(EcosystemStageStatus.DEBATED)
    queued = await repo.list_deep_reviews_by_stage("queued")

    assert {r.id for r in shallow} == {r1.id}
    assert {r.id for r in debated} == {r2.id}
    assert {r.id for r in queued} == {r3.id}


@pytest.mark.asyncio
async def test_explicit_completed_at_respected(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """显式传入的 completed_at 应被采用，方便回填。"""
    review = await repo.create_deep_review(
        EcosystemDeepReview(repo_id=sample_repo_id)
    )
    target_ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    updated = await repo.update_deep_review_stage(
        review.id,
        EcosystemStageStatus.SHALLOW_DONE,
        completed_at=target_ts,
    )
    assert updated.shallow_completed_at == target_ts
