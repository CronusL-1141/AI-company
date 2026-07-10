"""仓-标签多对多关联存储层单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """内存 SQLite 仓库用于测试。"""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def fixtures(repo: StorageRepository) -> dict[str, str]:
    """预置一个 repo_profile + 两个 tag，返回 id 映射。"""
    profile = EcosystemRepoProfile(
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        stars=50000,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile)
    p = await repo.get_ecosystem_profile("anthropics/claude-code")
    assert p is not None

    await repo.upsert_tag(
        EcosystemTag(name="memory_system", category=EcosystemTagCategory.CAPABILITY)
    )
    await repo.upsert_tag(
        EcosystemTag(name="python", category=EcosystemTagCategory.TECH_STACK)
    )
    tag_a = await repo.get_tag_by_name("memory_system")
    tag_b = await repo.get_tag_by_name("python")
    assert tag_a is not None and tag_b is not None

    return {"repo_id": p.id, "tag_a": tag_a.id, "tag_b": tag_b.id}


async def test_add_repo_tag_creates_association(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """添加新关联后能在 list 中看到。"""
    rt = EcosystemRepoTag(
        repo_id=fixtures["repo_id"],
        tag_id=fixtures["tag_a"],
        confidence=0.9,
        source=EcosystemTagSource.AUTO_LLM,
    )
    await repo.add_repo_tag(rt)

    rows = await repo.list_repo_tags(repo_id=fixtures["repo_id"])
    assert len(rows) == 1
    assert rows[0].tag_id == fixtures["tag_a"]
    assert rows[0].confidence == 0.9
    assert rows[0].source == EcosystemTagSource.AUTO_LLM


async def test_add_repo_tag_idempotent_updates(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """同 (repo_id, tag_id) 再次 add 会更新 confidence / source 而非新增。"""
    rt = EcosystemRepoTag(
        repo_id=fixtures["repo_id"],
        tag_id=fixtures["tag_a"],
        confidence=0.5,
        source=EcosystemTagSource.AUTO_RULE,
    )
    await repo.add_repo_tag(rt)

    updated = EcosystemRepoTag(
        repo_id=fixtures["repo_id"],
        tag_id=fixtures["tag_a"],
        confidence=0.95,
        source=EcosystemTagSource.MANUAL,
        agent_id="reviewer-1",
    )
    await repo.add_repo_tag(updated)

    rows = await repo.list_repo_tags(repo_id=fixtures["repo_id"])
    assert len(rows) == 1
    assert rows[0].confidence == 0.95
    assert rows[0].source == EcosystemTagSource.MANUAL
    assert rows[0].agent_id == "reviewer-1"


async def test_remove_repo_tag(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """移除关联后 list 不再包含；重复 remove 返回 False。"""
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=fixtures["repo_id"],
            tag_id=fixtures["tag_a"],
        )
    )

    removed = await repo.remove_repo_tag(fixtures["repo_id"], fixtures["tag_a"])
    assert removed is True

    rows = await repo.list_repo_tags(repo_id=fixtures["repo_id"])
    assert len(rows) == 0

    removed_again = await repo.remove_repo_tag(fixtures["repo_id"], fixtures["tag_a"])
    assert removed_again is False


async def test_list_repo_tags_filter_by_tag_id(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """按 tag_id 过滤可定位关联了某 tag 的所有 repo。"""
    await repo.add_repo_tag(
        EcosystemRepoTag(repo_id=fixtures["repo_id"], tag_id=fixtures["tag_a"])
    )
    await repo.add_repo_tag(
        EcosystemRepoTag(repo_id=fixtures["repo_id"], tag_id=fixtures["tag_b"])
    )

    rows_a = await repo.list_repo_tags(tag_id=fixtures["tag_a"])
    rows_b = await repo.list_repo_tags(tag_id=fixtures["tag_b"])
    assert len(rows_a) == 1 and rows_a[0].tag_id == fixtures["tag_a"]
    assert len(rows_b) == 1 and rows_b[0].tag_id == fixtures["tag_b"]


async def test_multiple_tags_per_repo(
    repo: StorageRepository, fixtures: dict[str, str]
) -> None:
    """单仓可关联多个不同 tag。"""
    await repo.add_repo_tag(
        EcosystemRepoTag(repo_id=fixtures["repo_id"], tag_id=fixtures["tag_a"])
    )
    await repo.add_repo_tag(
        EcosystemRepoTag(repo_id=fixtures["repo_id"], tag_id=fixtures["tag_b"])
    )

    rows = await repo.list_repo_tags(repo_id=fixtures["repo_id"])
    tag_ids = {r.tag_id for r in rows}
    assert tag_ids == {fixtures["tag_a"], fixtures["tag_b"]}
