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


# ================================================================
# A2-obs：接管留痕（辩论 503e07f1 议题A 裁决，观测两周后再议 A2-impl）
#
# 不加任何列、不改 schema——只在"前任还在位但租约已过期、被他人抢走"这一条
# 分支上打一条事件。判据是"有没有真的发生过交替"，没有事件就说明这条路从未走过，
# A2-impl（epoch 列）也就不必做。
# ================================================================


async def _takeover_events(repo: StorageRepository) -> list:
    return await repo.list_events(event_type="governance.lease_taken_over", limit=50)


async def test_takeover_from_expired_holder_emits_event(repo: StorageRepository) -> None:
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=0) is True
    await asyncio.sleep(0.01)
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True

    # 跨持久化边界读回：EventType 是 StrEnum，读端 EventType(x) 会对缺席成员抛
    # ValueError（29287f88 抓过同款潜伏崩溃），只有真查库才能证明枚举成员在场。
    events = await _takeover_events(repo)
    assert len(events) == 1
    data = events[0].data
    assert data["previous_holder"] == "api-111"
    assert data["new_holder"] == "api-222"
    assert data["previous_expires_at"]  # 过期时刻要留下来，才能算交替间隔


async def test_enum_member_present(repo: StorageRepository) -> None:
    """枚举成员必须真在 EventType 里——事件类型字符串对不上就是潜伏 ValueError。"""
    from aiteam.types import EventType

    assert EventType("governance.lease_taken_over") is EventType.GOVERNANCE_LEASE_TAKEN_OVER


async def test_fresh_acquire_emits_nothing(repo: StorageRepository) -> None:
    """无主认领不是接管：没有前任，就没有交替。"""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await _takeover_events(repo) == []


async def test_same_holder_renewal_emits_nothing(repo: StorageRepository) -> None:
    """续约不是接管——否则每 60s 一条事件，观测面直接被自己淹掉。"""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=0) is True
    await asyncio.sleep(0.01)
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await _takeover_events(repo) == []


async def test_losing_attempt_emits_nothing(repo: StorageRepository) -> None:
    """没抢到的那一方不留痕：事件描述的是既成事实，不是意图。"""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is True
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is False
    assert await _takeover_events(repo) == []


async def test_acquire_after_release_emits_nothing(repo: StorageRepository) -> None:
    """主动让出后的接手是交接不是接管——holder 已置空，无人被夺走。"""
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=600) is True
    assert await repo.release_governance_lease("api-111") is True
    assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True
    assert await _takeover_events(repo) == []


async def test_event_failure_never_blocks_the_lease(repo: StorageRepository, caplog) -> None:
    """观测挂了不能拖垮治理：租约照拿，但必须留一条 WARNING，不许静默吞掉。"""
    import logging
    from unittest import mock

    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=0) is True
    await asyncio.sleep(0.01)

    async def boom(*args, **kwargs):
        raise RuntimeError("events ledger down")

    with mock.patch.object(repo, "create_event", side_effect=boom):
        with caplog.at_level(logging.WARNING):
            assert await repo.try_acquire_governance_lease("api-222", ttl_seconds=60) is True

    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    # 租约本体不受影响：新持有者确实在位
    assert await repo.try_acquire_governance_lease("api-111", ttl_seconds=60) is False
