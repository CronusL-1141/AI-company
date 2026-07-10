"""v1.5.0-A: Profile 失败/活跃状态 helpers 单元测试。

覆盖：
- mark_profile_deleted / mark_profile_private / mark_profile_fetch_failure
- clear_profile_failure (复活机制 §3.3)
- update_profile_active_set
- update_profile_shallow_summary
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoProfile


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def sample_repo_id(repo: StorageRepository) -> str:
    profile = EcosystemRepoProfile(
        repo_full_name="anthropics/skills",
        name="skills",
        owner="anthropics",
        stars=12000,
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("anthropics/skills")
    assert fetched is not None
    return fetched.id


@pytest.mark.asyncio
async def test_mark_profile_deleted_sets_flags(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """删库标记应设 is_deleted=True / is_active=False / 写错误消息。"""
    updated = await repo.mark_profile_deleted(
        sample_repo_id, error_message="GitHub returned 404"
    )
    assert updated is not None
    assert updated.is_deleted is True
    assert updated.is_active is False
    assert updated.last_fetch_error == "GitHub returned 404"


@pytest.mark.asyncio
async def test_mark_profile_private_sets_flags(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """私密标记 is_private_now=True / is_active=False。"""
    updated = await repo.mark_profile_private(sample_repo_id)
    assert updated.is_private_now is True
    assert updated.is_active is False
    assert "403" in updated.last_fetch_error


@pytest.mark.asyncio
async def test_mark_profile_fetch_failure_increments_count(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """普通抓取失败累加 fetch_failure_count，不切换 deleted/private/active。"""
    u1 = await repo.mark_profile_fetch_failure(
        sample_repo_id, error_message="connection reset"
    )
    assert u1.fetch_failure_count == 1
    assert u1.is_deleted is False
    assert u1.is_active is True

    u2 = await repo.mark_profile_fetch_failure(
        sample_repo_id, error_message="timeout"
    )
    assert u2.fetch_failure_count == 2
    assert u2.last_fetch_error == "timeout"


@pytest.mark.asyncio
async def test_clear_profile_failure_resurrects(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """复活机制：删除/私密标记 + 失败计数清零，is_active 回 True。"""
    await repo.mark_profile_deleted(sample_repo_id, error_message="404")
    await repo.mark_profile_fetch_failure(sample_repo_id, error_message="404 again")

    cleared = await repo.clear_profile_failure(sample_repo_id)
    assert cleared.is_deleted is False
    assert cleared.is_private_now is False
    assert cleared.is_active is True
    assert cleared.last_fetch_error == ""
    assert cleared.fetch_failure_count == 0


@pytest.mark.asyncio
async def test_update_profile_active_set(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """活跃集刷新写 is_active + active_rank。"""
    updated = await repo.update_profile_active_set(
        sample_repo_id, is_active=True, active_rank=42
    )
    assert updated.is_active is True
    assert updated.active_rank == 42

    deactivated = await repo.update_profile_active_set(
        sample_repo_id, is_active=False, active_rank=None
    )
    assert deactivated.is_active is False
    assert deactivated.active_rank is None


@pytest.mark.asyncio
async def test_update_profile_shallow_summary_writes_timestamp(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """写浅扫总结自动更新 last_shallow_refreshed_at。"""
    before = datetime.now(tz=UTC)
    updated = await repo.update_profile_shallow_summary(
        sample_repo_id,
        shallow_summary="这是 anthropics/skills 的浅扫总结...",
    )
    assert updated.shallow_summary.startswith("这是")
    assert updated.last_shallow_refreshed_at is not None
    assert updated.last_shallow_refreshed_at >= before


@pytest.mark.asyncio
async def test_update_profile_shallow_summary_explicit_timestamp(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """显式 refreshed_at 应被采用。"""
    target = datetime(2025, 6, 1, tzinfo=UTC)
    updated = await repo.update_profile_shallow_summary(
        sample_repo_id,
        shallow_summary="历史回填总结",
        refreshed_at=target,
    )
    assert updated.last_shallow_refreshed_at == target


@pytest.mark.asyncio
async def test_mark_nonexistent_repo_returns_none(repo: StorageRepository) -> None:
    """不存在的 repo_id 返回 None，不抛异常。"""
    result = await repo.mark_profile_deleted("does-not-exist")
    assert result is None
