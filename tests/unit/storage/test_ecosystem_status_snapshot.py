"""v1.5.0-A: EcosystemRepoStatusSnapshot 单元测试 (append-only 快照表)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRepoProfile,
    EcosystemRepoStatusSnapshot,
    EcosystemScanRun,
    EcosystemScanStrategy,
)


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


@pytest_asyncio.fixture()
async def sample_scan_run_id(repo: StorageRepository) -> str:
    run = await repo.create_scan_run(
        EcosystemScanRun(strategy=EcosystemScanStrategy.INCREMENTAL)
    )
    return run.id


@pytest.mark.asyncio
async def test_create_status_snapshot_basic(
    repo: StorageRepository, sample_repo_id: str, sample_scan_run_id: str
) -> None:
    """创建快照后能按 repo_id 列出。"""
    snap = EcosystemRepoStatusSnapshot(
        repo_id=sample_repo_id,
        scan_run_id=sample_scan_run_id,
        stars=12345,
        is_active=True,
    )
    await repo.create_status_snapshot(snap)

    snapshots = await repo.list_status_snapshots(repo_id=sample_repo_id)
    assert len(snapshots) == 1
    assert snapshots[0].stars == 12345
    assert snapshots[0].is_active is True


@pytest.mark.asyncio
async def test_status_snapshot_append_only_history(
    repo: StorageRepository, sample_repo_id: str, sample_scan_run_id: str
) -> None:
    """每次写入都新增一行，按时间倒序返回（append-only）。"""
    for stars, is_active in [(1000, False), (5000, True), (8000, True)]:
        await repo.create_status_snapshot(
            EcosystemRepoStatusSnapshot(
                repo_id=sample_repo_id,
                scan_run_id=sample_scan_run_id,
                stars=stars,
                is_active=is_active,
                snapshot_at=datetime.now(tz=UTC),
            )
        )

    snapshots = await repo.list_status_snapshots(repo_id=sample_repo_id, limit=10)
    assert len(snapshots) == 3
    # 三个 stars 都被记录，append-only
    star_set = {s.stars for s in snapshots}
    assert star_set == {1000, 5000, 8000}


@pytest.mark.asyncio
async def test_status_snapshot_filter_by_scan_run(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """按 scan_run_id 过滤。"""
    run_a = await repo.create_scan_run(
        EcosystemScanRun(strategy=EcosystemScanStrategy.INCREMENTAL)
    )
    run_b = await repo.create_scan_run(
        EcosystemScanRun(strategy=EcosystemScanStrategy.FULL)
    )

    for run_id, stars in [(run_a.id, 100), (run_a.id, 200), (run_b.id, 300)]:
        await repo.create_status_snapshot(
            EcosystemRepoStatusSnapshot(
                repo_id=sample_repo_id,
                scan_run_id=run_id,
                stars=stars,
            )
        )

    snaps_a = await repo.list_status_snapshots(scan_run_id=run_a.id)
    snaps_b = await repo.list_status_snapshots(scan_run_id=run_b.id)

    assert len(snaps_a) == 2
    assert len(snaps_b) == 1
    assert snaps_b[0].stars == 300


@pytest.mark.asyncio
async def test_status_snapshot_summary_at_time_preserved(
    repo: StorageRepository, sample_repo_id: str, sample_scan_run_id: str
) -> None:
    """summary_at_time 保留当时的 shallow_summary 文本。"""
    snap = EcosystemRepoStatusSnapshot(
        repo_id=sample_repo_id,
        scan_run_id=sample_scan_run_id,
        stars=10000,
        summary_at_time="历史浅扫总结：这是一个 skills 框架...",
    )
    await repo.create_status_snapshot(snap)

    fetched = await repo.list_status_snapshots(repo_id=sample_repo_id)
    assert "skills 框架" in fetched[0].summary_at_time
