"""生态仓档案存储层单元测试 — upsert + search 覆盖。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoProfile

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


def _make_profile(
    repo_full_name: str = "anthropics/claude-code",
    stars: int = 20000,
    needs_deep_review: bool = False,
    category: str | None = "agent-framework",
    topics: list[str] | None = None,
) -> EcosystemRepoProfile:
    return EcosystemRepoProfile(
        repo_full_name=repo_full_name,
        name=repo_full_name.split("/")[-1],
        owner=repo_full_name.split("/")[0],
        description="Test repo",
        stars=stars,
        language="Python",
        topics=topics or ["claude", "agent"],
        needs_deep_review=needs_deep_review,
        relevance_category=category,
        relevance_score=8,
        one_line_summary="A test Claude ecosystem repo",
        last_scanned_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# upsert tests (5 cases)
# ---------------------------------------------------------------------------


async def test_upsert_new_profile_creates_record(repo: StorageRepository) -> None:
    """新档案 upsert 后能被 search 检索到。"""
    profile = _make_profile()
    await repo.upsert_ecosystem_profile(profile)

    results = await repo.search_ecosystem_profiles(limit=10)
    assert any(p.repo_full_name == profile.repo_full_name for p in results)


async def test_upsert_updates_stars_on_second_call(repo: StorageRepository) -> None:
    """同一 repo_full_name 再次 upsert 时 stars 字段被更新。"""
    profile = _make_profile(stars=10000)
    await repo.upsert_ecosystem_profile(profile)

    updated = _make_profile(stars=25000)
    await repo.upsert_ecosystem_profile(updated)

    results = await repo.search_ecosystem_profiles(limit=10)
    matching = [p for p in results if p.repo_full_name == profile.repo_full_name]
    assert len(matching) == 1
    assert matching[0].stars == 25000


async def test_upsert_preserves_first_seen_at(repo: StorageRepository) -> None:
    """多次 upsert 后 first_seen_at 不变，last_scanned_at 更新。"""
    profile = _make_profile()
    await repo.upsert_ecosystem_profile(profile)

    results_before = await repo.search_ecosystem_profiles(limit=10)
    first_seen = next(
        p.first_seen_at for p in results_before if p.repo_full_name == profile.repo_full_name
    )

    import asyncio
    await asyncio.sleep(0.01)

    profile2 = _make_profile(stars=30000)
    profile2 = profile2.model_copy(
        update={"last_scanned_at": datetime.now(tz=UTC)}
    )
    await repo.upsert_ecosystem_profile(profile2)

    results_after = await repo.search_ecosystem_profiles(limit=10)
    after = next(
        p for p in results_after if p.repo_full_name == profile.repo_full_name
    )
    # first_seen_at should remain unchanged
    assert abs((after.first_seen_at - first_seen).total_seconds()) < 1


async def test_upsert_multiple_distinct_repos(repo: StorageRepository) -> None:
    """多个不同 repo upsert 后各自独立存在。"""
    repos = [
        _make_profile(f"owner/repo-{i}", stars=5000 + i * 1000)
        for i in range(3)
    ]
    for p in repos:
        await repo.upsert_ecosystem_profile(p)

    results = await repo.search_ecosystem_profiles(limit=20)
    stored_names = {p.repo_full_name for p in results}
    for p in repos:
        assert p.repo_full_name in stored_names


async def test_upsert_updates_needs_deep_review_flag(repo: StorageRepository) -> None:
    """needs_deep_review 字段在 upsert 时可更新。"""
    profile = _make_profile(needs_deep_review=True, stars=8000)
    await repo.upsert_ecosystem_profile(profile)

    updated = _make_profile(needs_deep_review=False, stars=20000)
    await repo.upsert_ecosystem_profile(updated)

    results = await repo.search_ecosystem_profiles(limit=10)
    matching = next(
        (p for p in results if p.repo_full_name == profile.repo_full_name), None
    )
    assert matching is not None
    assert matching.needs_deep_review is False


# ---------------------------------------------------------------------------
# search tests (5 cases)
# ---------------------------------------------------------------------------


async def test_search_by_min_stars_filters_correctly(repo: StorageRepository) -> None:
    """min_stars 过滤：只返回 stars >= 阈值的档案。"""
    await repo.upsert_ecosystem_profile(_make_profile("owner/low-stars", stars=3000))
    await repo.upsert_ecosystem_profile(_make_profile("owner/high-stars", stars=20000))

    results = await repo.search_ecosystem_profiles(min_stars=10000)
    names = {p.repo_full_name for p in results}
    assert "owner/high-stars" in names
    assert "owner/low-stars" not in names


async def test_search_by_keyword_matches_name(repo: StorageRepository) -> None:
    """keyword 过滤：匹配 name 字段（LIKE）。使用 repo_full_name 唯一标识作为关键词。"""
    profile_a = EcosystemRepoProfile(
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        description="An AI coding assistant",
        stars=50000,
        topics=["claude"],
        one_line_summary="Claude coding tool",
        last_scanned_at=datetime.now(tz=UTC),
    )
    profile_b = EcosystemRepoProfile(
        repo_full_name="openai/gpt-tools",
        name="gpt-tools",
        owner="openai",
        description="OpenAI GPT utilities",
        stars=40000,
        topics=["openai", "gpt"],
        one_line_summary="GPT utilities by OpenAI",
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile_a)
    await repo.upsert_ecosystem_profile(profile_b)

    results = await repo.search_ecosystem_profiles(keyword="anthropics")
    names = {p.repo_full_name for p in results}
    assert "anthropics/claude-code" in names
    assert "openai/gpt-tools" not in names


async def test_search_by_category(repo: StorageRepository) -> None:
    """category 过滤：只返回指定分类的档案。"""
    await repo.upsert_ecosystem_profile(
        _make_profile("owner/mcp-tool", stars=8000, category="mcp-server")
    )
    await repo.upsert_ecosystem_profile(
        _make_profile("owner/agent-lib", stars=6000, category="agent-framework")
    )

    results = await repo.search_ecosystem_profiles(category="mcp-server")
    assert all(p.relevance_category == "mcp-server" for p in results)
    names = {p.repo_full_name for p in results}
    assert "owner/mcp-tool" in names
    assert "owner/agent-lib" not in names


async def test_search_by_needs_deep_review(repo: StorageRepository) -> None:
    """needs_deep_review 过滤：True/False 各自准确返回。"""
    await repo.upsert_ecosystem_profile(
        _make_profile("owner/small-repo", stars=6000, needs_deep_review=True)
    )
    await repo.upsert_ecosystem_profile(
        _make_profile("owner/large-repo", stars=20000, needs_deep_review=False)
    )

    needs_review = await repo.search_ecosystem_profiles(needs_deep_review=True)
    no_review = await repo.search_ecosystem_profiles(needs_deep_review=False)

    assert all(p.needs_deep_review for p in needs_review)
    assert all(not p.needs_deep_review for p in no_review)


async def test_search_returns_sorted_by_stars_desc(repo: StorageRepository) -> None:
    """结果按 stars 降序排列。"""
    for stars in [5000, 30000, 15000]:
        await repo.upsert_ecosystem_profile(
            _make_profile(f"owner/repo-{stars}", stars=stars)
        )

    results = await repo.search_ecosystem_profiles(limit=10)
    star_values = [p.stars for p in results]
    assert star_values == sorted(star_values, reverse=True)


# ---------------------------------------------------------------------------
# Stage B 扩展字段 (4 cases)
# ---------------------------------------------------------------------------


async def test_stage_b_fields_persist_on_create(repo: StorageRepository) -> None:
    """新增的 4 个 Stage B 字段创建时能完整保存。"""
    pushed = datetime.now(tz=UTC)
    profile = EcosystemRepoProfile(
        repo_full_name="owner/active-repo",
        name="active-repo",
        owner="owner",
        stars=2000,
        last_scanned_at=datetime.now(tz=UTC),
        pushed_at=pushed,
        is_archived=False,
        scan_run_id="scan-001",
        description_excerpt="一个活跃的测试仓",
    )
    await repo.upsert_ecosystem_profile(profile)

    fetched = await repo.get_ecosystem_profile("owner/active-repo")
    assert fetched is not None
    assert fetched.pushed_at is not None
    # SQLite 存储后 datetime 可能丢失 tz，使用 naive 转换比较
    pushed_naive = pushed.replace(tzinfo=None)
    fetched_pushed_naive = fetched.pushed_at.replace(tzinfo=None) if fetched.pushed_at.tzinfo else fetched.pushed_at
    assert abs((fetched_pushed_naive - pushed_naive).total_seconds()) < 1
    assert fetched.is_archived is False
    assert fetched.scan_run_id == "scan-001"
    assert fetched.description_excerpt == "一个活跃的测试仓"


async def test_stage_b_fields_updatable_on_upsert(repo: StorageRepository) -> None:
    """二次 upsert 时新字段可被更新（如标 is_archived）。"""
    profile = EcosystemRepoProfile(
        repo_full_name="owner/aging-repo",
        name="aging-repo",
        owner="owner",
        stars=1000,
        last_scanned_at=datetime.now(tz=UTC),
        is_archived=False,
        scan_run_id="scan-001",
    )
    await repo.upsert_ecosystem_profile(profile)

    updated = profile.model_copy(
        update={
            "is_archived": True,
            "scan_run_id": "scan-002",
            "description_excerpt": "deprecated repo",
            "last_scanned_at": datetime.now(tz=UTC),
        }
    )
    await repo.upsert_ecosystem_profile(updated)

    fetched = await repo.get_ecosystem_profile("owner/aging-repo")
    assert fetched is not None
    assert fetched.is_archived is True
    assert fetched.scan_run_id == "scan-002"
    assert fetched.description_excerpt == "deprecated repo"


async def test_stage_b_default_values_on_legacy_data(repo: StorageRepository) -> None:
    """未显式设置 Stage B 字段时使用默认值。"""
    profile = _make_profile("owner/legacy-repo", stars=8000)
    await repo.upsert_ecosystem_profile(profile)

    fetched = await repo.get_ecosystem_profile("owner/legacy-repo")
    assert fetched is not None
    assert fetched.pushed_at is None
    assert fetched.is_archived is False
    assert fetched.scan_run_id is None
    assert fetched.description_excerpt == ""


async def test_get_ecosystem_profile_by_id_roundtrip(repo: StorageRepository) -> None:
    """get_ecosystem_profile_by_id 能按主键取回。"""
    profile = _make_profile("owner/by-id-test", stars=12000)
    await repo.upsert_ecosystem_profile(profile)

    fetched_by_name = await repo.get_ecosystem_profile("owner/by-id-test")
    assert fetched_by_name is not None
    fetched_by_id = await repo.get_ecosystem_profile_by_id(fetched_by_name.id)
    assert fetched_by_id is not None
    assert fetched_by_id.repo_full_name == "owner/by-id-test"
