"""Stage E 扩展检索 + 全息详情 + facet 聚合存储层测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRelation,
    EcosystemRelationType,
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemScanRun,
    EcosystemScanStrategy,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
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


def _make_profile(
    repo_full_name: str,
    *,
    stars: int = 5000,
    language: str | None = "Python",
    category: str | None = "tooling",
    pushed_at: datetime | None = None,
    is_archived: bool = False,
    description_excerpt: str = "",
    relevance_score: int = 5,
) -> EcosystemRepoProfile:
    return EcosystemRepoProfile(
        repo_full_name=repo_full_name,
        name=repo_full_name.split("/")[-1],
        owner=repo_full_name.split("/")[0],
        description=f"Test repo {repo_full_name}",
        stars=stars,
        language=language,
        topics=["claude"],
        relevance_category=category,
        relevance_score=relevance_score,
        one_line_summary=f"Summary of {repo_full_name}",
        last_scanned_at=datetime.now(tz=UTC),
        pushed_at=pushed_at,
        is_archived=is_archived,
        description_excerpt=description_excerpt,
    )


async def _seed_basic(repo: StorageRepository) -> dict[str, EcosystemRepoProfile]:
    """种子若干仓档案，返回 name->profile 字典。"""
    now = datetime.now(tz=UTC)
    profiles = {
        "anthropics/claude-code": _make_profile(
            "anthropics/claude-code",
            stars=45000,
            language="TypeScript",
            category="skill-system",
            pushed_at=now - timedelta(days=2),
            relevance_score=10,
        ),
        "modelcontextprotocol/servers": _make_profile(
            "modelcontextprotocol/servers",
            stars=22000,
            language="Python",
            category="mcp-server",
            pushed_at=now - timedelta(days=5),
            relevance_score=9,
        ),
        "langchain/langgraph": _make_profile(
            "langchain/langgraph",
            stars=12000,
            language="Python",
            category="agent-framework",
            pushed_at=now - timedelta(days=10),
            relevance_score=7,
        ),
        "old/archived-repo": _make_profile(
            "old/archived-repo",
            stars=1500,
            language="Go",
            category="tooling",
            pushed_at=now - timedelta(days=400),
            is_archived=True,
            relevance_score=2,
        ),
        "tiny/utility": _make_profile(
            "tiny/utility",
            stars=800,
            language="Python",
            category="tooling",
            pushed_at=now - timedelta(days=30),
            relevance_score=3,
        ),
    }
    for p in profiles.values():
        await repo.upsert_ecosystem_profile(p)
    return profiles


# ---------------------------------------------------------------------------
# search_ecosystem_profiles_extended — basic filters (5)
# ---------------------------------------------------------------------------


async def test_extended_search_returns_total(repo: StorageRepository) -> None:
    """扩展检索返回 (rows, total) 元组，total 等于 limit 之前的命中数。"""
    await _seed_basic(repo)
    rows, total = await repo.search_ecosystem_profiles_extended(limit=2)
    assert total == 5
    assert len(rows) == 2


async def test_extended_search_language_filter(repo: StorageRepository) -> None:
    """language 过滤精确匹配。"""
    await _seed_basic(repo)
    rows, total = await repo.search_ecosystem_profiles_extended(language="Python", limit=10)
    names = {p.repo_full_name for p in rows}
    assert "modelcontextprotocol/servers" in names
    assert "langchain/langgraph" in names
    assert "tiny/utility" in names
    assert "anthropics/claude-code" not in names
    assert total == 3


async def test_extended_search_pushed_after_filter(repo: StorageRepository) -> None:
    """pushed_after 过滤：仅返回 pushed_at >= 阈值的仓。"""
    await _seed_basic(repo)
    cutoff = datetime.now(tz=UTC) - timedelta(days=15)
    rows, total = await repo.search_ecosystem_profiles_extended(
        pushed_after=cutoff, limit=10
    )
    names = {p.repo_full_name for p in rows}
    # 2/5/10 days 通过；30/400 不通过
    assert "anthropics/claude-code" in names
    assert "modelcontextprotocol/servers" in names
    assert "langchain/langgraph" in names
    assert "tiny/utility" not in names
    assert "old/archived-repo" not in names


async def test_extended_search_is_archived_filter(repo: StorageRepository) -> None:
    """is_archived=True 仅返回归档仓。"""
    await _seed_basic(repo)
    rows, _ = await repo.search_ecosystem_profiles_extended(
        is_archived=True, limit=10
    )
    assert len(rows) == 1
    assert rows[0].repo_full_name == "old/archived-repo"


async def test_extended_search_offset_pagination(repo: StorageRepository) -> None:
    """offset/limit 实现翻页，total 不变。"""
    await _seed_basic(repo)
    page1, total1 = await repo.search_ecosystem_profiles_extended(limit=2, offset=0)
    page2, total2 = await repo.search_ecosystem_profiles_extended(limit=2, offset=2)
    assert total1 == total2 == 5
    assert len(page1) == 2
    assert len(page2) == 2
    p1_names = {p.repo_full_name for p in page1}
    p2_names = {p.repo_full_name for p in page2}
    assert p1_names.isdisjoint(p2_names)


# ---------------------------------------------------------------------------
# Sort modes (3)
# ---------------------------------------------------------------------------


async def test_sort_stars_desc_default(repo: StorageRepository) -> None:
    """默认 sort=stars 按 star 降序。"""
    await _seed_basic(repo)
    rows, _ = await repo.search_ecosystem_profiles_extended(limit=10)
    star_seq = [p.stars for p in rows]
    assert star_seq == sorted(star_seq, reverse=True)


async def test_sort_recency_orders_by_pushed_at(repo: StorageRepository) -> None:
    """sort=recency 按 pushed_at 降序，None 排最后。"""
    await _seed_basic(repo)
    rows, _ = await repo.search_ecosystem_profiles_extended(sort="recency", limit=10)
    # 最新 pushed 应该在前
    assert rows[0].repo_full_name == "anthropics/claude-code"


async def test_sort_relevance_orders_by_relevance_score(
    repo: StorageRepository,
) -> None:
    """sort=relevance 按 relevance_score 降序。"""
    await _seed_basic(repo)
    rows, _ = await repo.search_ecosystem_profiles_extended(sort="relevance", limit=10)
    score_seq = [p.relevance_score for p in rows]
    assert score_seq == sorted(score_seq, reverse=True)


# ---------------------------------------------------------------------------
# tags filter (4)
# ---------------------------------------------------------------------------


async def test_tags_match_all_and_semantics(repo: StorageRepository) -> None:
    """tag_match_mode=all 要求所有 tag 都关联到 repo（AND 语义）。"""
    profiles = await _seed_basic(repo)
    target = profiles["modelcontextprotocol/servers"]

    tag_a = EcosystemTag(name="capability_a", category=EcosystemTagCategory.CAPABILITY)
    tag_b = EcosystemTag(name="capability_b", category=EcosystemTagCategory.CAPABILITY)
    await repo.upsert_tag(tag_a)
    await repo.upsert_tag(tag_b)
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=target.id, tag_id=tag_a.id, source=EcosystemTagSource.MANUAL
        )
    )
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=target.id, tag_id=tag_b.id, source=EcosystemTagSource.MANUAL
        )
    )
    # 仅给 langchain/langgraph 加 a 不加 b
    other = profiles["langchain/langgraph"]
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=other.id, tag_id=tag_a.id, source=EcosystemTagSource.MANUAL
        )
    )

    rows, total = await repo.search_ecosystem_profiles_extended(
        tags=["capability_a", "capability_b"], tag_match_mode="all", limit=10
    )
    names = {p.repo_full_name for p in rows}
    assert "modelcontextprotocol/servers" in names
    assert "langchain/langgraph" not in names
    assert total == 1


async def test_tags_match_any_or_semantics(repo: StorageRepository) -> None:
    """tag_match_mode=any OR 语义命中。"""
    profiles = await _seed_basic(repo)
    tag_a = EcosystemTag(name="x_capa", category=EcosystemTagCategory.CAPABILITY)
    tag_b = EcosystemTag(name="x_capb", category=EcosystemTagCategory.CAPABILITY)
    await repo.upsert_tag(tag_a)
    await repo.upsert_tag(tag_b)

    p1 = profiles["modelcontextprotocol/servers"]
    p2 = profiles["langchain/langgraph"]
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=p1.id, tag_id=tag_a.id, source=EcosystemTagSource.MANUAL
        )
    )
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=p2.id, tag_id=tag_b.id, source=EcosystemTagSource.MANUAL
        )
    )

    rows, total = await repo.search_ecosystem_profiles_extended(
        tags=["x_capa", "x_capb"], tag_match_mode="any", limit=10
    )
    names = {p.repo_full_name for p in rows}
    assert "modelcontextprotocol/servers" in names
    assert "langchain/langgraph" in names
    assert total == 2


async def test_tags_unknown_returns_empty(repo: StorageRepository) -> None:
    """传入完全未知的 tag 名 → 0 结果（短路）。"""
    await _seed_basic(repo)
    rows, total = await repo.search_ecosystem_profiles_extended(
        tags=["totally-unknown-tag"], tag_match_mode="all", limit=10
    )
    assert rows == []
    assert total == 0


async def test_tags_combined_with_other_filters(repo: StorageRepository) -> None:
    """tag 过滤与 min_stars 等基础过滤组合生效。"""
    profiles = await _seed_basic(repo)
    tag = EcosystemTag(name="combo_tag", category=EcosystemTagCategory.CAPABILITY)
    await repo.upsert_tag(tag)
    for p in profiles.values():
        await repo.add_repo_tag(
            EcosystemRepoTag(
                repo_id=p.id, tag_id=tag.id, source=EcosystemTagSource.MANUAL
            )
        )

    rows, total = await repo.search_ecosystem_profiles_extended(
        tags=["combo_tag"], min_stars=10000, limit=10
    )
    names = {p.repo_full_name for p in rows}
    # 仅 stars>=10000 的命中（claude-code/servers/langgraph）
    assert names == {
        "anthropics/claude-code",
        "modelcontextprotocol/servers",
        "langchain/langgraph",
    }
    assert total == 3


# ---------------------------------------------------------------------------
# get_ecosystem_profile_full (4)
# ---------------------------------------------------------------------------


async def test_get_full_returns_none_for_missing(repo: StorageRepository) -> None:
    """不存在的仓返回 None。"""
    result = await repo.get_ecosystem_profile_full(repo_full_name="ghost/none")
    assert result is None


async def test_get_full_includes_tags_with_names(repo: StorageRepository) -> None:
    """全息详情包含标签 + 标签名（join EcosystemTag 取 name/category）。"""
    profiles = await _seed_basic(repo)
    target = profiles["langchain/langgraph"]
    tag = EcosystemTag(name="agent_lib", category=EcosystemTagCategory.CAPABILITY)
    await repo.upsert_tag(tag)
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=target.id,
            tag_id=tag.id,
            confidence=0.9,
            source=EcosystemTagSource.AUTO_RULE,
        )
    )

    result = await repo.get_ecosystem_profile_full(
        repo_full_name="langchain/langgraph"
    )
    assert result is not None
    assert result["profile"].repo_full_name == "langchain/langgraph"
    assert any(
        t["name"] == "agent_lib" and t["confidence"] == 0.9 for t in result["tags"]
    )


async def test_get_full_includes_relations_from_and_to(
    repo: StorageRepository,
) -> None:
    """relations_from / relations_to 双向，含目标/起点仓 full_name。"""
    profiles = await _seed_basic(repo)
    src = profiles["modelcontextprotocol/servers"]
    tgt = profiles["anthropics/claude-code"]

    await repo.add_relation(
        EcosystemRelation(
            from_repo_id=src.id,
            to_repo_id=tgt.id,
            relation_type=EcosystemRelationType.INSPIRED_BY,
            evidence="MCP servers reference Claude tooling",
        )
    )

    full_src = await repo.get_ecosystem_profile_full(
        repo_full_name="modelcontextprotocol/servers"
    )
    full_tgt = await repo.get_ecosystem_profile_full(
        repo_full_name="anthropics/claude-code"
    )
    assert full_src is not None and full_tgt is not None

    rels_from = full_src["relations_from"]
    assert any(
        r["to_repo_full_name"] == "anthropics/claude-code"
        and r["relation_type"] == "inspired_by"
        for r in rels_from
    )

    rels_to = full_tgt["relations_to"]
    assert any(
        r["from_repo_full_name"] == "modelcontextprotocol/servers"
        for r in rels_to
    )


async def test_get_full_includes_deep_reviews_and_scan_run(
    repo: StorageRepository,
) -> None:
    """deep_reviews + scan_run 关联返回。"""
    profiles = await _seed_basic(repo)
    target = profiles["anthropics/claude-code"]

    scan = EcosystemScanRun(strategy=EcosystemScanStrategy.FULL, notes="seed")
    await repo.create_scan_run(scan)
    # 关联 scan_run_id 后重 upsert
    target_with_scan = target.model_copy(
        update={
            "scan_run_id": scan.id,
            "last_scanned_at": datetime.now(tz=UTC),
        }
    )
    await repo.upsert_ecosystem_profile(target_with_scan)

    review = EcosystemDeepReview(
        repo_id=target.id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        summary_md="Stage E review",
    )
    await repo.create_deep_review(review)

    full = await repo.get_ecosystem_profile_full(
        repo_full_name="anthropics/claude-code"
    )
    assert full is not None
    assert len(full["deep_reviews"]) >= 1
    assert full["scan_run"] is not None
    assert full["scan_run"].id == scan.id


# ---------------------------------------------------------------------------
# Facet counts (3)
# ---------------------------------------------------------------------------


async def test_facet_counts_categories(repo: StorageRepository) -> None:
    """category facet 聚合正确。"""
    await _seed_basic(repo)
    facets = await repo.compute_ecosystem_facet_counts()
    assert facets["category"]["tooling"] == 2
    assert facets["category"]["mcp-server"] == 1
    assert facets["category"]["agent-framework"] == 1
    assert facets["category"]["skill-system"] == 1


async def test_facet_counts_languages(repo: StorageRepository) -> None:
    """language facet 聚合正确。"""
    await _seed_basic(repo)
    facets = await repo.compute_ecosystem_facet_counts()
    assert facets["language"]["Python"] == 3
    assert facets["language"]["TypeScript"] == 1
    assert facets["language"]["Go"] == 1


async def test_facet_counts_archived(repo: StorageRepository) -> None:
    """archived facet 区分 true/false。"""
    await _seed_basic(repo)
    facets = await repo.compute_ecosystem_facet_counts()
    assert facets["archived"]["true"] == 1
    assert facets["archived"]["false"] == 4


# ---------------------------------------------------------------------------
# Performance smoke: 200 仓 × 50 random queries p95 < 50ms (1)
# ---------------------------------------------------------------------------


async def test_performance_search_under_50ms_p95(repo: StorageRepository) -> None:
    """种子 200 仓 + 50 次随机查询，p95 < 50ms。"""
    import random
    import time

    random.seed(42)

    # 创建 4 个 tag 用于覆盖测试
    tags = []
    for i in range(4):
        t = EcosystemTag(
            name=f"perf_tag_{i}", category=EcosystemTagCategory.CAPABILITY
        )
        await repo.upsert_tag(t)
        tags.append(t)

    # 种子 200 仓
    languages = ["Python", "TypeScript", "Go", "Rust", "Java"]
    categories = ["tooling", "agent-framework", "mcp-server", "skill-system", None]
    profiles: list[EcosystemRepoProfile] = []
    for i in range(200):
        p = _make_profile(
            f"perf-owner/repo-{i:03d}",
            stars=random.randint(100, 60000),
            language=random.choice(languages),
            category=random.choice(categories),
            pushed_at=datetime.now(tz=UTC)
            - timedelta(days=random.randint(0, 500)),
        )
        await repo.upsert_ecosystem_profile(p)
        profiles.append(p)

    # 随机给一半仓加 1-3 个 tag
    for p in profiles[:100]:
        for tag in random.sample(tags, k=random.randint(1, 3)):
            stored = await repo.get_ecosystem_profile(p.repo_full_name)
            await repo.add_repo_tag(
                EcosystemRepoTag(
                    repo_id=stored.id,
                    tag_id=tag.id,
                    source=EcosystemTagSource.AUTO_RULE,
                )
            )

    # 50 次随机查询
    durations: list[float] = []
    keywords = ["repo", "perf", "owner", ""]
    for _ in range(50):
        kw = random.choice(keywords)
        chosen_tags = random.sample(
            [t.name for t in tags], k=random.randint(0, 2)
        )
        sort = random.choice(["stars", "recency", "relevance"])

        t0 = time.perf_counter()
        await repo.search_ecosystem_profiles_extended(
            keyword=kw,
            tags=chosen_tags or None,
            tag_match_mode="any",
            sort=sort,
            limit=30,
        )
        durations.append((time.perf_counter() - t0) * 1000)

    durations.sort()
    p95 = durations[int(len(durations) * 0.95) - 1]
    # 50ms ceiling — SQLite in-memory should comfortably meet this
    assert p95 < 50.0, f"p95={p95:.2f}ms exceeds 50ms target"
