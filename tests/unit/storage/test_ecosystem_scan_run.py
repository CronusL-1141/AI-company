"""扫描批次记录存储层单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemScanRun, EcosystemScanStrategy


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """内存 SQLite 仓库用于测试。"""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


def _make_scan_run(
    strategy: EcosystemScanStrategy = EcosystemScanStrategy.INCREMENTAL,
    triggered_by: str = "manual",
) -> EcosystemScanRun:
    return EcosystemScanRun(
        strategy=strategy,
        triggered_by=triggered_by,
        notes="测试扫描批次",
    )


async def test_create_scan_run_persists(repo: StorageRepository) -> None:
    """创建扫描批次后能按 id 取回。"""
    run = _make_scan_run()
    await repo.create_scan_run(run)

    fetched = await repo.get_scan_run(run.id)
    assert fetched is not None
    assert fetched.strategy == EcosystemScanStrategy.INCREMENTAL
    assert fetched.triggered_by == "manual"


async def test_get_scan_run_missing_returns_none(repo: StorageRepository) -> None:
    """不存在的 id 返回 None。"""
    assert await repo.get_scan_run("non-existent-id") is None


async def test_update_scan_run_completion_stats(repo: StorageRepository) -> None:
    """扫描完成后更新统计字段。"""
    run = _make_scan_run()
    await repo.create_scan_run(run)

    completed_at = datetime.now(tz=UTC)
    updated = await repo.update_scan_run(
        run.id,
        completed_at=completed_at,
        duration_seconds=42.5,
        repos_added=10,
        repos_updated=20,
        repos_skipped=3,
        errors=["timeout: foo/bar"],
    )
    assert updated is not None
    assert updated.repos_added == 10
    assert updated.repos_updated == 20
    assert updated.repos_skipped == 3
    assert updated.duration_seconds == 42.5
    assert updated.errors == ["timeout: foo/bar"]


async def test_update_scan_run_missing_returns_none(repo: StorageRepository) -> None:
    """更新不存在的 scan_run 返回 None。"""
    assert await repo.update_scan_run("missing-id", repos_added=5) is None


async def test_list_scan_runs_filter_by_strategy(repo: StorageRepository) -> None:
    """按 strategy 过滤只返回对应批次。"""
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.INCREMENTAL))
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.FULL))
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.TRENDING))

    inc = await repo.list_scan_runs(strategy="incremental")
    full = await repo.list_scan_runs(strategy="full")
    trending = await repo.list_scan_runs(strategy="trending")
    assert len(inc) == 1 and inc[0].strategy == EcosystemScanStrategy.INCREMENTAL
    assert len(full) == 1 and full[0].strategy == EcosystemScanStrategy.FULL
    assert len(trending) == 1 and trending[0].strategy == EcosystemScanStrategy.TRENDING


async def test_list_scan_runs_no_filter_returns_all(repo: StorageRepository) -> None:
    """不传 strategy 返回所有批次。"""
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.INCREMENTAL))
    await repo.create_scan_run(_make_scan_run(EcosystemScanStrategy.TOPIC))

    rows = await repo.list_scan_runs()
    assert len(rows) >= 2
