"""治理 leader 租约单元测试 — D3 阶段C（审计 M50）。

验证 DB 原子认领语义：新租约可得、他人持有期内被拒、同 holder 续约、
过期后可被接管、单行表全程无增殖。
"""

from __future__ import annotations

import asyncio

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


async def test_fresh_acquire(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True


async def test_other_holder_blocked_while_valid(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is False


async def test_same_holder_renews(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True


async def test_expired_lease_taken_over(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=0) is True
    await asyncio.sleep(0.01)  # 让 now 严格晚于 expires_at（ISO 字符串字典序比较）
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True
    # 接管后，原持有者在新租约有效期内不能抢回
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is False


async def test_single_row_no_growth(repo: StorageRepository) -> None:
    from sqlalchemy import text

    from aiteam.storage.connection import get_session

    for holder in ("a", "b", "a", "c"):
        await repo.try_acquire_governance_lease(holder, ttl_seconds=0)
        await asyncio.sleep(0.005)
    async with get_session(repo._db_url) as session:
        result = await session.execute(text("SELECT COUNT(*) FROM governance_lease"))
        assert result.scalar_one() == 1


async def test_release_by_holder_frees_lease(repo: StorageRepository) -> None:
    """持有者主动释放后，另一实例无需等 TTL 即可立刻接管（77eb342 退出路径释放）。"""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=600) is True
    assert await repo.release_governance_lease("api-111") is True
    # 无需任何等待，另一实例直接抢到
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True


async def test_release_by_non_holder_is_noop(repo: StorageRepository) -> None:
    """非持有者释放是 no-op：不得替别人交出租约（如 89513 之于 82516 的残留）。"""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=600) is True
    assert await repo.release_governance_lease("api-999") is False
    # 原持有者租约仍然有效，别人仍抢不到
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is False


async def test_release_when_no_lease_row(repo: StorageRepository) -> None:
    """空表释放不炸、返回 False（退出路径 best-effort 语义）。"""
    assert await repo.release_governance_lease("api-111") is False
