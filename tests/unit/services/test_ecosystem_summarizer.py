"""Tests for EcosystemSummarizer — markdown briefing service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio

from aiteam.services.ecosystem_summarizer import (
    REPORT_TYPE_BY_TAG,
    REPORT_TYPE_HEALTH,
    REPORT_TYPE_TOP_N,
    REPORT_TYPE_WEEKLY,
    TOP_N_SORT_OPTIONS,
    EcosystemSummarizer,
    _short,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemScanRun,
    EcosystemScanStrategy,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


def _now() -> datetime:
    return datetime.now(tz=UTC)


async def _seed_profile(
    repo: StorageRepository,
    *,
    repo_full_name: str,
    stars: int = 1500,
    language: str | None = "Python",
    one_line: str = "demo summary",
    is_archived: bool = False,
    first_seen_offset_days: float = 0,
    last_scanned_offset_days: float = 0,
    topics: list[str] | None = None,
) -> EcosystemRepoProfile:
    now = _now()
    profile = EcosystemRepoProfile(
        repo_full_name=repo_full_name,
        name=repo_full_name.split("/")[-1],
        owner=repo_full_name.split("/")[0],
        description=f"description of {repo_full_name}",
        stars=stars,
        language=language,
        topics=topics or [],
        one_line_summary=one_line,
        first_seen_at=now - timedelta(days=first_seen_offset_days),
        last_scanned_at=now - timedelta(days=last_scanned_offset_days),
        is_archived=is_archived,
        relevance_category="agent-framework",
    )
    await repo.upsert_ecosystem_profile(profile)
    saved = await repo.get_ecosystem_profile(repo_full_name)
    assert saved is not None
    return saved


# ============================================================
# Constants exposure
# ============================================================


def test_top_n_sort_options_contains_expected() -> None:
    assert "stars" in TOP_N_SORT_OPTIONS
    assert "pushed_at" in TOP_N_SORT_OPTIONS
    assert "scan_freshness" in TOP_N_SORT_OPTIONS


def test_report_type_constants_namespaced() -> None:
    assert REPORT_TYPE_WEEKLY == "ecosystem-weekly"
    assert REPORT_TYPE_BY_TAG == "ecosystem-by-tag"
    assert REPORT_TYPE_TOP_N == "ecosystem-top-n"
    assert REPORT_TYPE_HEALTH == "ecosystem-health"


# ============================================================
# weekly_summary
# ============================================================


async def test_weekly_summary_empty_dataset_renders_zero_lines(
    repo: StorageRepository,
) -> None:
    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.weekly_summary()
    assert "# Ecosystem Weekly Briefing" in md
    assert "新入档: **0**" in md
    assert "本周无新增仓库" in md


async def test_weekly_summary_counts_new_vs_updated(
    repo: StorageRepository,
) -> None:
    # New: first_seen within window
    await _seed_profile(
        repo,
        repo_full_name="acme/new-one",
        stars=8000,
        first_seen_offset_days=1,
        last_scanned_offset_days=1,
    )
    # Updated: first_seen outside window, scanned within window
    await _seed_profile(
        repo,
        repo_full_name="acme/old-but-rescanned",
        stars=12000,
        first_seen_offset_days=30,
        last_scanned_offset_days=2,
    )
    # Stale (out of window entirely)
    await _seed_profile(
        repo,
        repo_full_name="acme/stale",
        stars=4000,
        first_seen_offset_days=60,
        last_scanned_offset_days=60,
    )

    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.weekly_summary(window_days=7)
    assert "新入档: **1**" in md
    assert "更新扫描: **1**" in md
    assert "acme/new-one" in md  # appears in top movers
    # acme/stale must not appear in top-mover row block
    top_movers_section = md.split("## Top Movers")[1].split("##")[0]
    assert "acme/stale" not in top_movers_section


async def test_weekly_summary_includes_recent_scan_runs(
    repo: StorageRepository,
) -> None:
    run = EcosystemScanRun(
        strategy=EcosystemScanStrategy.INCREMENTAL,
        repos_added=3,
        repos_updated=2,
        repos_skipped=1,
        duration_seconds=12.0,
    )
    await repo.create_scan_run(run)
    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.weekly_summary()
    assert "扫描批次: **1**" in md
    assert "incremental" in md


async def test_weekly_summary_lists_completed_deep_review(
    repo: StorageRepository,
) -> None:
    profile = await _seed_profile(
        repo, repo_full_name="acme/deep", stars=8000
    )
    review = EcosystemDeepReview(
        repo_id=profile.id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        completed_at=_now(),
        duration_seconds=42.0,
    )
    await repo.create_deep_review(review)
    # update_deep_review keeps status visible in list_deep_reviews(status=...)
    await repo.update_deep_review(
        review.id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        completed_at=_now(),
    )

    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.weekly_summary()
    assert "完成深扫: **1**" in md
    assert review.id in md


# ============================================================
# by_tag_summary
# ============================================================


async def _seed_tag(repo: StorageRepository, name: str) -> EcosystemTag:
    tag = EcosystemTag(name=name, category=EcosystemTagCategory.CAPABILITY)
    await repo.upsert_tag(tag)
    fetched = await repo.get_tag_by_name(name)
    assert fetched is not None
    return fetched


async def test_by_tag_summary_empty_tag_returns_helper_message(
    repo: StorageRepository,
) -> None:
    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.by_tag_summary("")
    assert "必须提供 tag 名称" in md


async def test_by_tag_summary_no_matches(
    repo: StorageRepository,
) -> None:
    await _seed_tag(repo, "memory_system")
    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.by_tag_summary("memory_system")
    assert "没有仓被标注" in md


async def test_by_tag_summary_returns_tagged_repos_sorted_by_stars(
    repo: StorageRepository,
) -> None:
    tag = await _seed_tag(repo, "memory_system")
    p_low = await _seed_profile(repo, repo_full_name="acme/low", stars=1500)
    p_high = await _seed_profile(repo, repo_full_name="acme/high", stars=20000)
    for p in (p_low, p_high):
        await repo.add_repo_tag(
            EcosystemRepoTag(
                repo_id=p.id,
                tag_id=tag.id,
                source=EcosystemTagSource.MANUAL,
                confidence=1.0,
            )
        )

    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.by_tag_summary("memory_system")
    high_pos = md.find("acme/high")
    low_pos = md.find("acme/low")
    assert high_pos != -1 and low_pos != -1
    assert high_pos < low_pos  # high stars first
    assert "共 **2** 仓" in md


async def test_by_tag_summary_filters_archived_by_default(
    repo: StorageRepository,
) -> None:
    tag = await _seed_tag(repo, "memory_system")
    archived = await _seed_profile(
        repo,
        repo_full_name="acme/archived",
        stars=1000,
        is_archived=True,
    )
    active = await _seed_profile(
        repo, repo_full_name="acme/active", stars=2000
    )
    for p in (archived, active):
        await repo.add_repo_tag(
            EcosystemRepoTag(
                repo_id=p.id,
                tag_id=tag.id,
                source=EcosystemTagSource.MANUAL,
            )
        )

    summarizer = EcosystemSummarizer(repo)
    md_default = await summarizer.by_tag_summary("memory_system")
    assert "acme/active" in md_default
    assert "acme/archived" not in md_default

    md_with_archived = await summarizer.by_tag_summary(
        "memory_system", include_archived=True
    )
    assert "acme/archived" in md_with_archived


async def test_by_tag_summary_links_deep_review_id(
    repo: StorageRepository,
) -> None:
    tag = await _seed_tag(repo, "memory_system")
    profile = await _seed_profile(
        repo, repo_full_name="acme/deepreview", stars=2000
    )
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=profile.id,
            tag_id=tag.id,
            source=EcosystemTagSource.MANUAL,
        )
    )
    review = EcosystemDeepReview(
        repo_id=profile.id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        completed_at=_now(),
    )
    await repo.create_deep_review(review)
    await repo.update_deep_review(
        review.id, status=EcosystemDeepReviewStatus.COMPLETED
    )

    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.by_tag_summary("memory_system")
    assert review.id in md


# ============================================================
# top_n_summary
# ============================================================


async def test_top_n_summary_default_sort_is_stars(
    repo: StorageRepository,
) -> None:
    await _seed_profile(repo, repo_full_name="acme/a", stars=500)
    await _seed_profile(repo, repo_full_name="acme/b", stars=5000)
    await _seed_profile(repo, repo_full_name="acme/c", stars=2500)

    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.top_n_summary(n=3, sort="stars")
    # b should appear first
    pos_b = md.find("acme/b")
    pos_a = md.find("acme/a")
    assert pos_b != -1 and pos_a != -1
    assert pos_b < pos_a


async def test_top_n_summary_invalid_sort_falls_back_to_stars(
    repo: StorageRepository,
) -> None:
    await _seed_profile(repo, repo_full_name="acme/single", stars=1234)
    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.top_n_summary(sort="banana", n=1)
    assert "sort=`stars`" in md  # fell back


async def test_top_n_summary_caps_n_at_100(
    repo: StorageRepository,
) -> None:
    await _seed_profile(repo, repo_full_name="acme/x", stars=1000)
    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.top_n_summary(n=10000)
    # Only one repo seeded; n cap should not break rendering
    assert "acme/x" in md


async def test_top_n_summary_empty_returns_helper_message(
    repo: StorageRepository,
) -> None:
    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.top_n_summary(category="nonexistent", n=5)
    assert "没有匹配仓库" in md


async def test_top_n_summary_scan_freshness_sort(
    repo: StorageRepository,
) -> None:
    await _seed_profile(
        repo,
        repo_full_name="acme/fresh",
        stars=1000,
        last_scanned_offset_days=0,
    )
    await _seed_profile(
        repo,
        repo_full_name="acme/stale",
        stars=5000,  # higher stars but stale
        last_scanned_offset_days=30,
    )
    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.top_n_summary(n=2, sort="scan_freshness")
    pos_fresh = md.find("acme/fresh")
    pos_stale = md.find("acme/stale")
    assert pos_fresh != -1 and pos_stale != -1
    assert pos_fresh < pos_stale  # fresher first regardless of stars


# ============================================================
# health_summary
# ============================================================


async def test_health_summary_empty_state(repo: StorageRepository) -> None:
    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.health_summary()
    assert "仓档案总数: **0**" in md
    assert "标签字典: **0** 项" in md
    assert "失活仓占比: **0.0%**" in md


async def test_health_summary_aggregates_counts_correctly(
    repo: StorageRepository,
) -> None:
    tag = await _seed_tag(repo, "memory_system")
    p1 = await _seed_profile(
        repo, repo_full_name="acme/one", stars=1500
    )
    p2 = await _seed_profile(
        repo,
        repo_full_name="acme/two-archived",
        stars=900,
        is_archived=True,
    )
    _ = await _seed_profile(        repo, repo_full_name="acme/three-no-tag", stars=2000
    )
    # tag p1 and p2
    for p in (p1, p2):
        await repo.add_repo_tag(
            EcosystemRepoTag(
                repo_id=p.id, tag_id=tag.id, source=EcosystemTagSource.MANUAL
            )
        )

    run = EcosystemScanRun(
        strategy=EcosystemScanStrategy.INCREMENTAL,
        repos_added=1,
    )
    await repo.create_scan_run(run)

    # D5: create_deep_review 强制按 stage_status 派生 status（唯一真源
    # types.STAGE_TO_STATUS），completed 行经权威轴 stage=shallow_done 构造。
    from aiteam.types import EcosystemStageStatus

    review = EcosystemDeepReview(
        repo_id=p1.id, stage_status=EcosystemStageStatus.SHALLOW_DONE
    )
    await repo.create_deep_review(review)

    summarizer = EcosystemSummarizer(repo)
    md = await summarizer.health_summary()
    assert "仓档案总数: **3**" in md
    assert "失活仓占比: **33.3%**" in md
    assert "0 标签仓占比: **33.3%**" in md  # only p3 lacks tags
    assert "标签字典: **1** 项" in md
    assert "扫描批次: **1** 次" in md
    assert "深扫报告: **1** 份" in md
    assert "completed=1" in md
    assert "memory_system" in md


# ============================================================
# Helper functions
# ============================================================


def test_short_truncates_long_text() -> None:
    long_text = "abc" * 200
    assert len(_short(long_text)) <= 91


def test_short_handles_none() -> None:
    assert _short(None) == "—"


def test_short_replaces_pipe_to_avoid_md_table_break() -> None:
    out = _short("a | b")
    assert "\\|" in out
