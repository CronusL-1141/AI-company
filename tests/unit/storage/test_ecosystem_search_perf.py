"""K1 — Ecosystem search performance regression tests.

These tests guard against the production p95=2057ms incident by:

1. Seeding 2000 profiles + 5000 repo_tag edges in a project-scoped repo.
2. Asserting EXPLAIN QUERY PLAN uses the composite K1 indexes (no SCAN).
3. Asserting actual wall-clock p95 stays well below the regression budget.

The thresholds here are CI-friendly (looser than the production 50ms target)
so the test does not flake on slow CI workers but will catch a 10x regression.

Run:
    python -m pytest tests/unit/storage/test_ecosystem_search_perf.py -v
"""

from __future__ import annotations

import random
import statistics
import time
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import text

from aiteam.storage.connection import close_db, get_session
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)

PROJECT_ID = "p-perftest"
N_PROFILES = 2000
N_TAGS = 20
EDGES_PER_REPO = 3
QUERY_BUDGET_P95_MS = 250.0  # generous CI budget; production target is < 50ms


@pytest_asyncio.fixture(scope="module")
async def seeded_repo() -> StorageRepository:
    """Module-scoped fixture: seed once, share across tests."""
    repo = StorageRepository(
        db_url="sqlite+aiosqlite:///:memory:",
        project_scope=PROJECT_ID,
    )
    await repo.init_db()

    rng = random.Random(42)
    languages = ["Python", "TypeScript", "Go", "Rust", "Java"]
    categories = ["framework", "tooling", "agent", "memory", "core"]

    # Seed profiles
    for i in range(N_PROFILES):
        await repo.upsert_ecosystem_profile(
            EcosystemRepoProfile(
                repo_full_name=f"owner{i % 50}/repo-{i:05d}",
                name=f"repo-{i:05d}",
                owner=f"owner{i % 50}",
                description=f"library doing thing-{i % 100}",
                stars=rng.randint(100, 100_000),
                language=rng.choice(languages),
                topics=["claude", "ai"],
                relevance_category=rng.choice(categories),
                relevance_score=rng.randint(0, 100),
                pushed_at=datetime.now(tz=UTC) - timedelta(days=rng.randint(0, 365)),
                is_archived=rng.random() < 0.05,
                description_excerpt=f"excerpt-{i}",
            )
        )

    # Seed tag dictionary
    tags: list[EcosystemTag] = []
    for i in range(N_TAGS):
        t = EcosystemTag(
            name=f"capability-{i:03d}",
            category=EcosystemTagCategory.CAPABILITY,
        )
        await repo.upsert_tag(t)
        tags.append(t)

    # Seed edges
    profiles, _ = await repo.search_ecosystem_profiles_extended(limit=N_PROFILES)
    for p in profiles:
        chosen = rng.sample(tags, k=EDGES_PER_REPO)
        for tag in chosen:
            await repo.add_repo_tag(
                EcosystemRepoTag(
                    repo_id=p.id,
                    tag_id=tag.id,
                    confidence=1.0,
                    source=EcosystemTagSource.MANUAL,
                )
            )

    yield repo
    await close_db()


# ---------------------------------------------------------------------------
# EXPLAIN QUERY PLAN — verify composite indexes are picked
# ---------------------------------------------------------------------------


async def test_explain_search_uses_project_stars_index(seeded_repo: StorageRepository) -> None:
    """K1 索引: project_id+stars 复合索引覆盖默认 stars 排序。"""
    async with get_session(seeded_repo._db_url) as session:
        rows = (
            await session.execute(
                text(
                    f"EXPLAIN QUERY PLAN SELECT * FROM ecosystem_repo_profiles "
                    f"WHERE project_id='{PROJECT_ID}' "
                    "ORDER BY stars DESC LIMIT 50"
                )
            )
        ).all()
    plan = " ".join(str(r) for r in rows)
    assert "ix_ecosystem_profiles_project_stars" in plan, (
        f"expected K1 composite index in plan, got:\n{plan}"
    )
    # No full SCAN over the table itself
    assert "SCAN ecosystem_repo_profiles" not in plan


async def test_explain_category_lang_uses_composite_index(
    seeded_repo: StorageRepository,
) -> None:
    """K1 索引: 同时过滤 category+language 走对应复合索引。"""
    async with get_session(seeded_repo._db_url) as session:
        rows = (
            await session.execute(
                text(
                    f"EXPLAIN QUERY PLAN SELECT * FROM ecosystem_repo_profiles "
                    f"WHERE project_id='{PROJECT_ID}' "
                    "AND relevance_category='tooling' AND language='Python' "
                    "ORDER BY stars DESC LIMIT 50"
                )
            )
        ).all()
    plan = " ".join(str(r) for r in rows)
    # SQLite picks one of the K1 composite indexes (cat or lang variant).
    assert any(
        idx in plan
        for idx in (
            "ix_ecosystem_profiles_project_lang_stars",
            "ix_ecosystem_profiles_project_category_stars",
        )
    ), f"expected K1 composite index, got:\n{plan}"


async def test_explain_pushed_at_sort_uses_composite_index(
    seeded_repo: StorageRepository,
) -> None:
    """K1 索引: pushed_at 排序走 (project_id, pushed_at) 索引避免 TEMP B-TREE。"""
    async with get_session(seeded_repo._db_url) as session:
        rows = (
            await session.execute(
                text(
                    f"EXPLAIN QUERY PLAN SELECT * FROM ecosystem_repo_profiles "
                    f"WHERE project_id='{PROJECT_ID}' "
                    "ORDER BY pushed_at DESC LIMIT 50"
                )
            )
        ).all()
    plan = " ".join(str(r) for r in rows)
    assert "ix_ecosystem_profiles_project_pushed" in plan, (
        f"expected K1 pushed_at index, got:\n{plan}"
    )


# ---------------------------------------------------------------------------
# Wall-clock regression — 50 random searches, p95 must stay under budget
# ---------------------------------------------------------------------------


async def _percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile (e.g. p=0.95 for p95)."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(len(s) * p) - 1))
    return s[idx]


async def test_search_p95_within_budget(seeded_repo: StorageRepository) -> None:
    """50 次随机 search 的 p95 必须远低于生产事故的 2057ms。

    使用 250ms 预算覆盖 CI 慢机；目标产品要求 < 50ms。
    """
    rng = random.Random(7)
    keywords = ["thing-1", "thing-99", "agent", "core", "rare"]

    durations_ms: list[float] = []
    for _ in range(50):
        t0 = time.perf_counter()
        await seeded_repo.search_ecosystem_profiles_extended(
            keyword=rng.choice(keywords),
            limit=50,
        )
        durations_ms.append((time.perf_counter() - t0) * 1000)

    p95 = await _percentile(durations_ms, 0.95)
    p50 = statistics.median(durations_ms)
    assert p95 < QUERY_BUDGET_P95_MS, (
        f"p95={p95:.1f}ms exceeded budget {QUERY_BUDGET_P95_MS}ms (p50={p50:.1f}ms). "
        "K1 perf regression!"
    )


async def test_facet_counts_p95_within_budget(seeded_repo: StorageRepository) -> None:
    """facet_counts 单次扫描重写：50 次跑下来 p95 必须在预算内。"""
    durations_ms: list[float] = []
    for _ in range(50):
        t0 = time.perf_counter()
        await seeded_repo.compute_ecosystem_facet_counts(min_stars=500)
        durations_ms.append((time.perf_counter() - t0) * 1000)

    p95 = await _percentile(durations_ms, 0.95)
    assert p95 < QUERY_BUDGET_P95_MS, (
        f"facet p95={p95:.1f}ms exceeded budget {QUERY_BUDGET_P95_MS}ms. "
        "facet_counts perf regression!"
    )


async def test_tags_empty_does_not_drop_results(seeded_repo: StorageRepository) -> None:
    """K1 bug fix: tags=[] / tags=None 不应进入 EXISTS 子查询而误返回 0。

    搜索带 tags=[] 必须等价于不传 tags（命中所有 N_PROFILES 行）。
    """
    rows_no_tags, total_no_tags = await seeded_repo.search_ecosystem_profiles_extended(
        limit=10
    )
    rows_empty_list, total_empty_list = await seeded_repo.search_ecosystem_profiles_extended(
        tags=[], limit=10
    )
    rows_none, total_none = await seeded_repo.search_ecosystem_profiles_extended(
        tags=None, limit=10
    )
    assert total_no_tags == N_PROFILES
    assert total_empty_list == N_PROFILES
    assert total_none == N_PROFILES
    assert len(rows_empty_list) == 10
    assert len(rows_none) == 10
