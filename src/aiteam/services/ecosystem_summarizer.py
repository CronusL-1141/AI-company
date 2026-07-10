"""Ecosystem summarizer — generate human-readable research briefings.

Builds four flavours of markdown summary on top of Stage B-F data:

- ``weekly_summary`` — last-7-days digest: new / updated / deep-scanned /
  archived repos plus star-growth movers.
- ``by_tag_summary`` — every repo carrying ``tag``, sorted by stars, with
  per-row stars / language / one-line positioning + linked deep-review.
- ``top_n_summary`` — Top-N table with switchable sort
  (``stars`` / ``pushed_at`` / ``scan_freshness``).
- ``health_summary`` — platform self-check covering profile count,
  ScanRun cadence, deep-review coverage, tag coverage, archive ratio.

Periodic scheduling
-------------------
This module deliberately does NOT auto-register a CronCreate at import
time. To run ``ecosystem_summary_weekly`` every Sunday at 22:00 UTC the
operator/Leader must invoke ``scheduler_create`` manually:

    scheduler_create(
        name="ecosystem_summary_weekly",
        interval="7 days",
        action_type="emit_event",
        action_config='{"event_type": "ecosystem.summary.weekly", "data": {}}',
        description="Weekly Claude ecosystem briefing",
    )

A subscriber to ``ecosystem.summary.weekly`` then calls the
``ecosystem_summary_weekly`` MCP tool. Auto-saving the produced markdown
into the report database is performed by the MCP layer, not the service —
the service stays pure (string in / string out) so it remains trivial to
unit test.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
    EcosystemScanRun,
)

logger = logging.getLogger(__name__)


# ============================================================
# Sort / report-type constants
# ============================================================

TOP_N_SORT_OPTIONS: tuple[str, ...] = ("stars", "pushed_at", "scan_freshness")

REPORT_TYPE_WEEKLY: str = "ecosystem-weekly"
REPORT_TYPE_BY_TAG: str = "ecosystem-by-tag"
REPORT_TYPE_TOP_N: str = "ecosystem-top-n"
REPORT_TYPE_HEALTH: str = "ecosystem-health"


# ============================================================
# Result containers
# ============================================================


@dataclass
class WeeklyStats:
    """Counters used to build the weekly briefing body."""

    window_days: int
    window_start: datetime
    window_end: datetime
    new_profiles: list[EcosystemRepoProfile] = field(default_factory=list)
    updated_profiles: list[EcosystemRepoProfile] = field(default_factory=list)
    completed_deep_reviews: list[EcosystemDeepReview] = field(default_factory=list)
    archived_profiles: list[EcosystemRepoProfile] = field(default_factory=list)
    top_movers: list[EcosystemRepoProfile] = field(default_factory=list)
    scan_runs: list[EcosystemScanRun] = field(default_factory=list)


@dataclass
class HealthStats:
    """Counters used by the platform self-check summary."""

    total_profiles: int = 0
    total_scan_runs: int = 0
    total_deep_reviews: int = 0
    deep_reviews_by_status: dict[str, int] = field(default_factory=dict)
    archived_profiles: int = 0
    profiles_with_zero_tags: int = 0
    avg_tags_per_profile: float = 0.0
    total_tags_in_dictionary: int = 0
    top_tags: list[tuple[str, int]] = field(default_factory=list)
    last_scan_run_at: datetime | None = None


# ============================================================
# Service
# ============================================================


class EcosystemSummarizer:
    """High-level read-only summary service for the ecosystem index.

    All public methods return markdown strings ready to be passed
    straight into ``report_save``. Internally each method aggregates
    via repository helpers that already collapse N+1 joins, so callers
    only pay one read per summary.
    """

    def __init__(
        self,
        repo: StorageRepository,
        project_id: str = "",
    ) -> None:
        """初始化简报服务。

        Args:
            repo: 数据访问层。
            project_id: 可选项目作用域；空时透传 repo._project_scope。
                所有读取均限定于此项目。
        """
        self._repo = repo
        self._project_id = project_id or repo._project_scope or ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def weekly_summary(
        self,
        *,
        now: datetime | None = None,
        window_days: int = 7,
        top_movers_limit: int = 5,
    ) -> str:
        """Build the past-week digest as a markdown report."""
        end = now or datetime.now(tz=UTC)
        start = end - timedelta(days=max(window_days, 1))

        all_profiles = await self._repo.search_ecosystem_profiles(
            limit=2000, project_id=self._project_id or None
        )
        new = [p for p in all_profiles if _within(p.first_seen_at, start, end)]
        updated = [
            p
            for p in all_profiles
            if _within(p.last_scanned_at, start, end)
            and not _within(p.first_seen_at, start, end)
        ]
        archived = [p for p in all_profiles if p.is_archived]

        deep_reviews = await self._repo.list_deep_reviews(
            status=EcosystemDeepReviewStatus.COMPLETED.value,
            limit=200,
            project_id=self._project_id or None,
        )
        completed_deep = [
            r
            for r in deep_reviews
            if r.completed_at is not None and _within(r.completed_at, start, end)
        ]

        scan_runs = await self._repo.list_scan_runs(
            limit=50, project_id=self._project_id or None
        )
        recent_scan_runs = [
            r for r in scan_runs if _within(r.started_at, start, end)
        ]

        top_movers = sorted(new, key=lambda p: p.stars, reverse=True)[
            :top_movers_limit
        ]

        stats = WeeklyStats(
            window_days=window_days,
            window_start=start,
            window_end=end,
            new_profiles=new,
            updated_profiles=updated,
            completed_deep_reviews=completed_deep,
            archived_profiles=archived,
            top_movers=top_movers,
            scan_runs=recent_scan_runs,
        )
        return _render_weekly(stats)

    async def by_tag_summary(
        self,
        tag: str,
        *,
        include_archived: bool = False,
        limit: int = 200,
    ) -> str:
        """List every repo carrying ``tag`` ordered by stars."""
        if not tag:
            return _empty_section(
                title="Ecosystem — By Tag",
                reason="必须提供 tag 名称",
            )

        profiles, _ = await self._repo.search_ecosystem_profiles_extended(
            tags=[tag],
            tag_match_mode="all",
            sort="stars",
            limit=limit,
            offset=0,
            project_id=self._project_id or None,
        )

        if not include_archived:
            profiles = [p for p in profiles if not p.is_archived]

        if not profiles:
            return _empty_section(
                title=f"Ecosystem — Tag `{tag}`",
                reason=f"没有仓被标注为 `{tag}`（include_archived={include_archived}）",
            )

        # Pull deep-review IDs in a single batch — N+1-free fetch
        deep_review_map = await self._collect_latest_deep_reviews(profiles)

        return _render_by_tag(
            tag=tag,
            include_archived=include_archived,
            profiles=profiles,
            deep_review_map=deep_review_map,
        )

    async def top_n_summary(
        self,
        *,
        category: str = "",
        n: int = 10,
        sort: str = "stars",
    ) -> str:
        """Return a markdown Top-N table for the chosen sort key."""
        if sort not in TOP_N_SORT_OPTIONS:
            sort = "stars"

        n = max(1, min(n, 100))

        repo_sort = "stars" if sort == "stars" else "recency"
        profiles, total = await self._repo.search_ecosystem_profiles_extended(
            category=category,
            sort=repo_sort,
            limit=max(n * 3, n),  # over-fetch so re-sort by scan_freshness has data
            offset=0,
            project_id=self._project_id or None,
        )

        if sort == "scan_freshness":
            profiles = sorted(
                profiles,
                key=lambda p: p.last_scanned_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )

        profiles = profiles[:n]

        if not profiles:
            return _empty_section(
                title="Ecosystem — Top N",
                reason=(
                    f"没有匹配仓库（category={category or '(all)'}, "
                    f"sort={sort}, n={n}）"
                ),
            )

        return _render_top_n(
            category=category,
            sort=sort,
            n=n,
            total_pool=total,
            profiles=profiles,
        )

    async def health_summary(self) -> str:
        """Generate the platform self-check markdown."""
        stats = await self._collect_health_stats()
        return _render_health(stats)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _collect_latest_deep_reviews(
        self, profiles: Iterable[EcosystemRepoProfile]
    ) -> dict[str, EcosystemDeepReview]:
        """Return repo_id -> latest deep_review (status COMPLETED preferred).

        Pulls all completed deep-reviews once (cheap LIMIT 500) and indexes
        in memory rather than hitting the DB once per repo.
        """
        latest: dict[str, EcosystemDeepReview] = {}
        rows = await self._repo.list_deep_reviews(
            status=EcosystemDeepReviewStatus.COMPLETED.value,
            limit=500,
            project_id=self._project_id or None,
        )
        for row in rows:
            existing = latest.get(row.repo_id)
            if existing is None or _newer(row.created_at, existing.created_at):
                latest[row.repo_id] = row

        # Filter to the profile set so the returned map stays small.
        wanted = {p.id for p in profiles}
        return {rid: r for rid, r in latest.items() if rid in wanted}

    async def _collect_health_stats(self) -> HealthStats:
        """Aggregate counters in a single pass — N+1-free."""
        all_profiles = await self._repo.search_ecosystem_profiles(
            limit=10000, project_id=self._project_id or None
        )
        scan_runs = await self._repo.list_scan_runs(
            limit=200, project_id=self._project_id or None
        )

        stats = HealthStats(
            total_profiles=len(all_profiles),
            total_scan_runs=len(scan_runs),
            archived_profiles=sum(1 for p in all_profiles if p.is_archived),
            last_scan_run_at=scan_runs[0].started_at if scan_runs else None,
        )

        deep_reviews = await self._repo.list_deep_reviews(
            limit=2000, project_id=self._project_id or None
        )
        stats.total_deep_reviews = len(deep_reviews)
        per_status: dict[str, int] = defaultdict(int)
        for r in deep_reviews:
            per_status[
                r.status.value if hasattr(r.status, "value") else str(r.status)
            ] += 1
        stats.deep_reviews_by_status = dict(per_status)

        # Tag aggregation: pull all repo_tag rows once + tag dictionary once.
        repo_tag_rows = await self._repo.list_repo_tags(
            limit=20000, project_id=self._project_id or None
        )
        tag_dict = {t.id: t.name for t in await self._repo.list_tags(limit=500)}
        stats.total_tags_in_dictionary = len(tag_dict)

        per_repo_tag_count: dict[str, int] = defaultdict(int)
        per_tag_repo_count: dict[str, int] = defaultdict(int)
        for row in repo_tag_rows:
            per_repo_tag_count[row.repo_id] += 1
            tname = tag_dict.get(row.tag_id)
            if tname:
                per_tag_repo_count[tname] += 1

        if all_profiles:
            stats.profiles_with_zero_tags = sum(
                1 for p in all_profiles if per_repo_tag_count.get(p.id, 0) == 0
            )
            stats.avg_tags_per_profile = round(
                sum(per_repo_tag_count.get(p.id, 0) for p in all_profiles)
                / len(all_profiles),
                2,
            )

        stats.top_tags = sorted(
            per_tag_repo_count.items(), key=lambda kv: kv[1], reverse=True
        )[:10]
        return stats


# ============================================================
# Markdown renderers (pure functions for deterministic tests)
# ============================================================


def _empty_section(*, title: str, reason: str) -> str:
    return f"# {title}\n\n_{reason}_\n"


def _render_weekly(stats: WeeklyStats) -> str:
    lines: list[str] = []
    lines.append("# Ecosystem Weekly Briefing")
    lines.append("")
    lines.append(
        f"_窗口: {_iso_date(stats.window_start)} → {_iso_date(stats.window_end)} "
        f"({stats.window_days} 天)_"
    )
    lines.append("")
    lines.append("## Highlights")
    lines.append("")
    lines.append(f"- 新入档: **{len(stats.new_profiles)}** 仓")
    lines.append(f"- 更新扫描: **{len(stats.updated_profiles)}** 仓")
    lines.append(f"- 完成深扫: **{len(stats.completed_deep_reviews)}** 仓")
    lines.append(f"- 失活仓总数: **{len(stats.archived_profiles)}** 仓")
    lines.append(f"- 扫描批次: **{len(stats.scan_runs)}** 次")
    lines.append("")

    lines.append("## Top Movers (按 stars 排序，本周新增)")
    lines.append("")
    if not stats.top_movers:
        lines.append("_本周无新增仓库_")
    else:
        lines.append("| Stars | Repo | Language | 一句话定位 |")
        lines.append("|-------|------|----------|-----------|")
        for p in stats.top_movers:
            lines.append(
                f"| {p.stars:,} | [{p.repo_full_name}](https://github.com/{p.repo_full_name}) "
                f"| {p.language or '-'} | {_short(p.one_line_summary or p.description)} |"
            )
    lines.append("")

    lines.append("## 完成深扫 (本周)")
    lines.append("")
    if not stats.completed_deep_reviews:
        lines.append("_本周无深扫完成_")
    else:
        for r in stats.completed_deep_reviews:
            lines.append(
                f"- review_id=`{r.id}` repo_id=`{r.repo_id}` "
                f"duration={r.duration_seconds:.0f}s"
            )
    lines.append("")

    lines.append("## 扫描批次")
    lines.append("")
    if not stats.scan_runs:
        lines.append("_本周未触发扫描_")
    else:
        lines.append("| started_at | strategy | added | updated | skipped | duration |")
        lines.append("|------------|----------|-------|---------|---------|----------|")
        for run in stats.scan_runs:
            lines.append(
                f"| {_iso_dt(run.started_at)} "
                f"| {run.strategy.value if hasattr(run.strategy, 'value') else run.strategy} "
                f"| {run.repos_added} | {run.repos_updated} | {run.repos_skipped} "
                f"| {run.duration_seconds:.0f}s |"
            )
    lines.append("")
    return "\n".join(lines)


def _render_by_tag(
    *,
    tag: str,
    include_archived: bool,
    profiles: list[EcosystemRepoProfile],
    deep_review_map: dict[str, EcosystemDeepReview],
) -> str:
    lines: list[str] = []
    lines.append(f"# Ecosystem — Tag `{tag}`")
    lines.append("")
    lines.append(
        f"_共 **{len(profiles)}** 仓 · include_archived={include_archived}_"
    )
    lines.append("")
    lines.append("| Stars | Repo | Language | 一句话定位 | DeepReview |")
    lines.append("|-------|------|----------|-----------|------------|")
    for p in profiles:
        review = deep_review_map.get(p.id)
        review_cell = f"`{review.id}`" if review else "—"
        lines.append(
            f"| {p.stars:,} | [{p.repo_full_name}](https://github.com/{p.repo_full_name}) "
            f"| {p.language or '-'} | {_short(p.one_line_summary or p.description)} "
            f"| {review_cell} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_top_n(
    *,
    category: str,
    sort: str,
    n: int,
    total_pool: int,
    profiles: list[EcosystemRepoProfile],
) -> str:
    title_cat = category or "all categories"
    lines: list[str] = []
    lines.append(f"# Ecosystem Top {len(profiles)} — sort=`{sort}`")
    lines.append("")
    lines.append(
        f"_category={title_cat} · 备选池={total_pool} · 实际展示={len(profiles)}_"
    )
    lines.append("")
    if sort == "pushed_at":
        lines.append("| # | Repo | PushedAt | Stars | Language | 一句话定位 |")
        lines.append("|---|------|----------|-------|----------|-----------|")
    elif sort == "scan_freshness":
        lines.append(
            "| # | Repo | LastScannedAt | Stars | Language | 一句话定位 |"
        )
        lines.append("|---|------|---------------|-------|----------|-----------|")
    else:  # stars
        lines.append("| # | Repo | Stars | Language | 一句话定位 |")
        lines.append("|---|------|-------|----------|-----------|")

    for idx, p in enumerate(profiles, start=1):
        if sort == "pushed_at":
            lines.append(
                f"| {idx} | [{p.repo_full_name}](https://github.com/{p.repo_full_name}) "
                f"| {_iso_dt(p.pushed_at)} | {p.stars:,} | {p.language or '-'} "
                f"| {_short(p.one_line_summary or p.description)} |"
            )
        elif sort == "scan_freshness":
            lines.append(
                f"| {idx} | [{p.repo_full_name}](https://github.com/{p.repo_full_name}) "
                f"| {_iso_dt(p.last_scanned_at)} | {p.stars:,} | {p.language or '-'} "
                f"| {_short(p.one_line_summary or p.description)} |"
            )
        else:
            lines.append(
                f"| {idx} | [{p.repo_full_name}](https://github.com/{p.repo_full_name}) "
                f"| {p.stars:,} | {p.language or '-'} "
                f"| {_short(p.one_line_summary or p.description)} |"
            )
    lines.append("")
    return "\n".join(lines)


def _render_health(stats: HealthStats) -> str:
    archive_pct = (
        round(stats.archived_profiles / stats.total_profiles * 100, 1)
        if stats.total_profiles
        else 0.0
    )
    zero_tag_pct = (
        round(stats.profiles_with_zero_tags / stats.total_profiles * 100, 1)
        if stats.total_profiles
        else 0.0
    )
    lines: list[str] = []
    lines.append("# Ecosystem Platform Health")
    lines.append("")
    lines.append("## 数据规模")
    lines.append("")
    lines.append(f"- 仓档案总数: **{stats.total_profiles}**")
    lines.append(f"- 标签字典: **{stats.total_tags_in_dictionary}** 项")
    lines.append(f"- 扫描批次: **{stats.total_scan_runs}** 次")
    lines.append(f"- 深扫报告: **{stats.total_deep_reviews}** 份")
    if stats.deep_reviews_by_status:
        breakdown = ", ".join(
            f"{k}={v}" for k, v in sorted(stats.deep_reviews_by_status.items())
        )
        lines.append(f"  - 状态分布: {breakdown}")
    lines.append("")
    lines.append("## 健康度")
    lines.append("")
    lines.append(f"- 失活仓占比: **{archive_pct}%** ({stats.archived_profiles}/{stats.total_profiles})")
    lines.append(
        f"- 0 标签仓占比: **{zero_tag_pct}%** "
        f"({stats.profiles_with_zero_tags}/{stats.total_profiles})"
    )
    lines.append(f"- 平均标签数 / 仓: **{stats.avg_tags_per_profile}**")
    if stats.last_scan_run_at:
        age = datetime.now(tz=UTC) - _ensure_tz(stats.last_scan_run_at)
        lines.append(
            f"- 最近一次扫描: {_iso_dt(stats.last_scan_run_at)} "
            f"({int(age.total_seconds() / 3600)} 小时前)"
        )
    else:
        lines.append("- 最近一次扫描: _无_")
    lines.append("")
    if stats.top_tags:
        lines.append("## Top 标签 (按关联仓数)")
        lines.append("")
        lines.append("| 标签 | 关联仓数 |")
        lines.append("|------|---------|")
        for name, count in stats.top_tags:
            lines.append(f"| `{name}` | {count} |")
        lines.append("")
    return "\n".join(lines)


# ============================================================
# Misc helpers
# ============================================================


def _within(value: datetime | None, start: datetime, end: datetime) -> bool:
    if value is None:
        return False
    v = _ensure_tz(value)
    return start <= v <= end


def _ensure_tz(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _newer(a: datetime | None, b: datetime | None) -> bool:
    if a is None:
        return False
    if b is None:
        return True
    return _ensure_tz(a) > _ensure_tz(b)


def _iso_date(value: datetime) -> str:
    return _ensure_tz(value).date().isoformat()


def _iso_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    return _ensure_tz(value).strftime("%Y-%m-%d %H:%M")


def _short(text: str | None, limit: int = 90) -> str:
    if not text:
        return "—"
    cleaned = text.replace("\n", " ").replace("|", "\\|").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


__all__ = [
    "EcosystemSummarizer",
    "HealthStats",
    "REPORT_TYPE_BY_TAG",
    "REPORT_TYPE_HEALTH",
    "REPORT_TYPE_TOP_N",
    "REPORT_TYPE_WEEKLY",
    "TOP_N_SORT_OPTIONS",
    "WeeklyStats",
]
