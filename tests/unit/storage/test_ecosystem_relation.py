"""仓与仓引用关系存储层单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRelation,
    EcosystemRelationType,
    EcosystemRepoProfile,
)


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """内存 SQLite 仓库用于测试。"""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _make_profiles(repo: StorageRepository, count: int = 3) -> list[str]:
    """快速建若干 repo profile，返回其 id 列表。"""
    ids: list[str] = []
    for i in range(count):
        p = EcosystemRepoProfile(
            repo_full_name=f"owner/repo-{i}",
            name=f"repo-{i}",
            owner="owner",
            stars=1000 + i * 100,
            last_scanned_at=datetime.now(tz=UTC),
        )
        await repo.upsert_ecosystem_profile(p)
        fetched = await repo.get_ecosystem_profile(f"owner/repo-{i}")
        assert fetched is not None
        ids.append(fetched.id)
    return ids


async def test_add_relation_persists(repo: StorageRepository) -> None:
    """添加关联后能在 list 中查到。"""
    ids = await _make_profiles(repo, count=2)
    rel = EcosystemRelation(
        from_repo_id=ids[0],
        to_repo_id=ids[1],
        relation_type=EcosystemRelationType.INSPIRED_BY,
        evidence="README mentions",
        confidence=0.8,
    )
    await repo.add_relation(rel)

    rows = await repo.list_relations(from_repo_id=ids[0])
    assert len(rows) == 1
    assert rows[0].relation_type == EcosystemRelationType.INSPIRED_BY
    assert rows[0].evidence == "README mentions"


async def test_list_relations_filter_by_type(repo: StorageRepository) -> None:
    """按 relation_type 过滤只返回对应类型。"""
    ids = await _make_profiles(repo, count=3)
    await repo.add_relation(
        EcosystemRelation(
            from_repo_id=ids[0],
            to_repo_id=ids[1],
            relation_type=EcosystemRelationType.FORKS,
        )
    )
    await repo.add_relation(
        EcosystemRelation(
            from_repo_id=ids[0],
            to_repo_id=ids[2],
            relation_type=EcosystemRelationType.EXTENDS,
        )
    )

    forks = await repo.list_relations(relation_type="forks")
    extends = await repo.list_relations(relation_type="extends")
    assert len(forks) == 1
    assert len(extends) == 1
    assert forks[0].relation_type == EcosystemRelationType.FORKS


async def test_list_relations_filter_by_to_repo(repo: StorageRepository) -> None:
    """按 to_repo_id 过滤可查找谁引用了这个仓。"""
    ids = await _make_profiles(repo, count=3)
    await repo.add_relation(
        EcosystemRelation(
            from_repo_id=ids[0],
            to_repo_id=ids[2],
            relation_type=EcosystemRelationType.DEPENDS_ON,
        )
    )
    await repo.add_relation(
        EcosystemRelation(
            from_repo_id=ids[1],
            to_repo_id=ids[2],
            relation_type=EcosystemRelationType.DEPENDS_ON,
        )
    )

    rows = await repo.list_relations(to_repo_id=ids[2])
    assert len(rows) == 2
    assert {r.from_repo_id for r in rows} == {ids[0], ids[1]}


async def test_remove_relation(repo: StorageRepository) -> None:
    """删除关联后 list 不再包含；重复删除返回 False。"""
    ids = await _make_profiles(repo, count=2)
    rel = EcosystemRelation(
        from_repo_id=ids[0],
        to_repo_id=ids[1],
        relation_type=EcosystemRelationType.COMPETES,
    )
    await repo.add_relation(rel)

    assert await repo.remove_relation(rel.id) is True
    rows = await repo.list_relations(from_repo_id=ids[0])
    assert len(rows) == 0

    assert await repo.remove_relation(rel.id) is False


async def test_multiple_relations_same_pair_allowed(repo: StorageRepository) -> None:
    """同一对 (from, to) 但不同 relation_type 应允许共存。"""
    ids = await _make_profiles(repo, count=2)
    await repo.add_relation(
        EcosystemRelation(
            from_repo_id=ids[0],
            to_repo_id=ids[1],
            relation_type=EcosystemRelationType.INSPIRED_BY,
        )
    )
    await repo.add_relation(
        EcosystemRelation(
            from_repo_id=ids[0],
            to_repo_id=ids[1],
            relation_type=EcosystemRelationType.COMPETES,
        )
    )

    rows = await repo.list_relations(from_repo_id=ids[0], to_repo_id=ids[1])
    types = {r.relation_type for r in rows}
    assert types == {
        EcosystemRelationType.INSPIRED_BY,
        EcosystemRelationType.COMPETES,
    }
