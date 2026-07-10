"""Ecosystem scanner service — periodic / incremental GitHub repo scanning.

Provides EcosystemScanner that:
- Runs configurable GitHub topic / keyword queries via injected gh_search callable
- Applies secondary filters (owner blacklist + description keyword whitelist)
- Records each scan as an EcosystemScanRun with errors collected (graceful degradation)
- Skips repos last_scanned_at < refresh_window_days (incremental strategy)
- Marks pushed_at > archive_threshold_days as is_archived=True

Designed for dependency injection: GitHub data source and StorageRepository are
both injected so tests can mock without touching subprocess or DB.

Periodic scheduling
-------------------
The scheduler tool is exposed via MCP `scheduler_create`. To register a weekly
ecosystem scan (Monday 02:00 every week), call:

    scheduler_create(
        name="ecosystem_scan_weekly",
        interval="7 days",
        action_type="emit_event",
        action_config='{"event_type": "ecosystem.scan.periodic",'
                      ' "data": {"strategy": "incremental"}}',
        description="Weekly Claude ecosystem scan (incremental)",
    )

A subscriber to the `ecosystem.scan.periodic` event then calls the
`ecosystem_scan_periodic` MCP tool with the supplied strategy. We deliberately
do NOT auto-register the cron at import time — the user/Leader decides whether
the workspace should run automated scans.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRepoEvent,
    EcosystemRepoProfile,
    EcosystemScanRun,
    EcosystemScanStrategy,
)

logger = logging.getLogger(__name__)


# ============================================================
# Configuration
# ============================================================


# Default ecosystem keyword whitelist — at least one must appear in description / topics / name.
_DEFAULT_DESCRIPTION_KEYWORDS: tuple[str, ...] = (
    "claude",
    "anthropic",
    "mcp",
    "agent",
    "llm",
    "skill",
    "orchestrat",
    "autonom",
    "language model",
    "ai assist",
)


# Default owner blacklist — high-stars but irrelevant tutorials / awesome-lists.
_DEFAULT_OWNER_BLACKLIST: tuple[str, ...] = (
    "Snailclimb",  # JavaGuide tutorial
    "CronusL-1141",  # own repos
)


# Built-in topic / keyword query plan for default scans.
DEFAULT_QUERIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("", ("claude-code",)),
    ("", ("mcp",)),
    ("", ("mcp-server",)),
    ("", ("claude-agent",)),
    ("claude", ("agent-framework",)),
    ("claude", ("ai-agents",)),
    ("claude code plugin", ()),
    ("anthropic agent", ()),
)


@dataclass
class FilterConfig:
    """Secondary filter configuration applied after GitHub returns results."""

    owner_blacklist: list[str] = field(default_factory=lambda: list(_DEFAULT_OWNER_BLACKLIST))
    description_keywords: list[str] = field(
        default_factory=lambda: list(_DEFAULT_DESCRIPTION_KEYWORDS)
    )
    min_stars: int = 1000
    refresh_window_days: int = 7  # incremental: skip repos scanned within window
    archive_threshold_days: int = 365  # mark is_archived if last push older than this
    require_description_keyword: bool = True

    @classmethod
    def from_env(cls) -> FilterConfig:
        """Build config from environment overrides.

        Recognised env vars:
          ECOSYSTEM_OWNER_BLACKLIST: comma-separated owner logins
          ECOSYSTEM_KEYWORDS: comma-separated description keywords
          ECOSYSTEM_MIN_STARS: int
          ECOSYSTEM_REFRESH_DAYS: int
          ECOSYSTEM_ARCHIVE_DAYS: int
        """
        cfg = cls()
        if raw := os.environ.get("ECOSYSTEM_OWNER_BLACKLIST"):
            cfg.owner_blacklist = [s.strip() for s in raw.split(",") if s.strip()]
        if raw := os.environ.get("ECOSYSTEM_KEYWORDS"):
            cfg.description_keywords = [s.strip().lower() for s in raw.split(",") if s.strip()]
        if raw := os.environ.get("ECOSYSTEM_MIN_STARS"):
            try:
                cfg.min_stars = int(raw)
            except ValueError:
                pass
        if raw := os.environ.get("ECOSYSTEM_REFRESH_DAYS"):
            try:
                cfg.refresh_window_days = int(raw)
            except ValueError:
                pass
        if raw := os.environ.get("ECOSYSTEM_ARCHIVE_DAYS"):
            try:
                cfg.archive_threshold_days = int(raw)
            except ValueError:
                pass
        return cfg


# ============================================================
# Filter helpers
# ============================================================


def _matches_keyword_whitelist(repo: dict[str, Any], keywords: list[str]) -> bool:
    """Return True if at least one keyword appears in description / name / topics."""
    if not keywords:
        return True
    blob = " ".join(
        [
            (repo.get("description") or "").lower(),
            (repo.get("name") or "").lower(),
            " ".join(repo.get("topics") or []).lower(),
        ]
    )
    return any(kw.lower() in blob for kw in keywords)


def _is_blacklisted_owner(repo: dict[str, Any], blacklist: list[str]) -> bool:
    """Case-insensitive owner login match against blacklist."""
    owner = (repo.get("owner") or "").lower()
    return any(b.lower() == owner for b in blacklist if b)


def _classify_archived(pushed_at: datetime | None, threshold_days: int) -> bool:
    """Return True when last push is older than threshold."""
    if pushed_at is None:
        return False
    cutoff = datetime.now(tz=UTC) - timedelta(days=threshold_days)
    pushed_compare = pushed_at if pushed_at.tzinfo else pushed_at.replace(tzinfo=UTC)
    return pushed_compare < cutoff


# ============================================================
# Scanner
# ============================================================


# Type for injected gh search function — returns parsed repo dicts.
# Each dict shape:
#   {repo_full_name, name, owner, description, stars, language, topics,
#    homepage, last_commit_at(datetime|None), pushed_at(datetime|None),
#    relevance_category, relevance_score, one_line_summary, needs_deep_review}
GhSearchFunc = Callable[[str, int, list[str] | None], Awaitable[list[dict[str, Any]]]]


@dataclass
class ScanResult:
    """Scanner outcome — used for both ScanRun update and tool return."""

    run_id: str
    strategy: str
    scanned: int
    new_profiles: int
    updated_profiles: int
    skipped: int
    archived_marked: int
    errors: list[str]
    duration_seconds: float
    per_query_stats: dict[str, int]
    category_distribution: dict[str, int]


class EcosystemScanner:
    """Periodic / incremental scanner for the Claude ecosystem.

    Args:
        repo: StorageRepository for persistence.
        gh_search: Async callable that returns parsed repo dicts for (keyword, min_stars, topics).
        config: FilterConfig (defaults to FilterConfig.from_env()).
        project_id: 可选项目作用域；非空时所有写入会附带此 project_id，
            读取也限定于同项目。空字符串表示透传 repo 的 _project_scope。
        on_new_profile: Optional async hook invoked once per **newly-archived**
            profile (i.e. ``existing is None`` at upsert time). Wired to the
            v1.5.0-B Stage 0 ``EcosystemShallowQueueWorker.enqueue_repo`` so
            new repos get an immediate shallow-scan dispatch. Failures are
            logged but never abort the scan.
    """

    def __init__(
        self,
        repo: StorageRepository,
        gh_search: GhSearchFunc,
        config: FilterConfig | None = None,
        project_id: str = "",
        on_new_profile: (
            Callable[[EcosystemRepoProfile], Awaitable[Any]] | None
        ) = None,
    ) -> None:
        self._repo = repo
        self._gh_search = gh_search
        self._config = config or FilterConfig.from_env()
        self._project_id = project_id or repo._project_scope or ""
        self._on_new_profile = on_new_profile

    @property
    def config(self) -> FilterConfig:
        return self._config

    async def scan(
        self,
        strategy: EcosystemScanStrategy = EcosystemScanStrategy.INCREMENTAL,
        queries: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
        triggered_by: str = "manual",
        agent_id: str | None = None,
        notes: str = "",
    ) -> ScanResult:
        """Run a full scan cycle, recording start/finish in EcosystemScanRun."""
        scan_run = EcosystemScanRun(
            strategy=strategy,
            triggered_by=triggered_by,
            agent_id=agent_id,
            notes=notes,
            project_id=self._project_id or None,
        )
        await self._repo.create_scan_run(
            scan_run, project_id=self._project_id or None
        )

        start = time.time()
        errors: list[str] = []
        per_query_stats: dict[str, int] = {}
        all_repos: dict[str, dict[str, Any]] = {}
        # P1.C-1: track all queries that found each repo (repo_full_name → set of query strings)
        repo_matched_queries: dict[str, list[str]] = {}
        active_queries = queries if queries is not None else DEFAULT_QUERIES

        for keyword, topics in active_queries:
            qkey = f"keyword={keyword!r} topics={list(topics)}"
            # Build a compact human-readable query string for storage
            if topics and keyword:
                matched_query = f"{keyword} " + " ".join(f"topic:{t}" for t in topics)
            elif topics:
                matched_query = " ".join(f"topic:{t}" for t in topics)
            else:
                matched_query = keyword
            try:
                items = await self._gh_search(keyword, self._config.min_stars, list(topics))
            except Exception as exc:  # graceful degradation — collect errors, keep going
                errors.append(f"gh_search failed for {qkey}: {exc!s}")
                per_query_stats[qkey] = 0
                continue

            count = 0
            for item in items:
                if not self._passes_filters(item):
                    continue
                fn = item.get("repo_full_name", "")
                if not fn:
                    continue
                if fn not in all_repos:
                    all_repos[fn] = item
                    count += 1
                # P1.C-1: record every query that hit this repo (union)
                if fn not in repo_matched_queries:
                    repo_matched_queries[fn] = []
                if matched_query and matched_query not in repo_matched_queries[fn]:
                    repo_matched_queries[fn].append(matched_query)
            per_query_stats[qkey] = count

        # Persist profiles — skip recently-scanned ones for incremental strategy
        new_count = 0
        updated_count = 0
        skipped = 0
        archived_marked = 0
        metadata_changed_count = 0

        for repo_data in all_repos.values():
            try:
                fn = repo_data["repo_full_name"]
                existing = await self._repo.get_ecosystem_profile(
                    fn, project_id=self._project_id or None
                )
                if (
                    strategy == EcosystemScanStrategy.INCREMENTAL
                    and existing is not None
                    and self._is_within_refresh_window(existing.last_scanned_at)
                ):
                    skipped += 1
                    # Still update discovered_via_queries for skipped repos
                    for mq in repo_matched_queries.get(fn, []):
                        try:
                            await self._repo.update_repo_discovered_queries_by_name(
                                fn, mq, project_id=self._project_id or None
                            )
                        except Exception:
                            pass
                    continue

                pushed_at = repo_data.get("pushed_at")
                is_archived = _classify_archived(
                    pushed_at, self._config.archive_threshold_days
                )
                if is_archived:
                    archived_marked += 1

                profile = self._build_profile(repo_data, scan_run.id, is_archived, existing)
                if self._project_id:
                    profile.project_id = self._project_id
                # P1.C-1: pre-populate discovered_via_queries from this scan's matched queries
                matched_qs = repo_matched_queries.get(fn, [])
                if matched_qs:
                    if existing and existing.discovered_via_queries:
                        merged = list(existing.discovered_via_queries)
                        for mq in matched_qs:
                            if mq not in merged:
                                merged.append(mq)
                        profile.discovered_via_queries = merged
                    else:
                        profile.discovered_via_queries = list(matched_qs)
                await self._repo.upsert_ecosystem_profile(
                    profile, project_id=self._project_id or None
                )
                if existing is None:
                    new_count += 1
                    # v1.5.0-B: notify Stage 0 queue worker so the new
                    # repo gets an immediate shallow-scan dispatch. Look
                    # up the persisted row to obtain the assigned id.
                    persisted: EcosystemRepoProfile | None = None
                    if self._on_new_profile is not None:
                        try:
                            persisted = await self._repo.get_ecosystem_profile(
                                fn, project_id=self._project_id or None
                            )
                            if persisted is not None:
                                await self._on_new_profile(persisted)
                        except Exception as hook_exc:
                            errors.append(
                                f"on_new_profile {fn}: {hook_exc!s}"
                            )
                    # v1.6.0: write 'discovered' event
                    try:
                        if persisted is None:
                            persisted = await self._repo.get_ecosystem_profile(
                                fn, project_id=self._project_id or None
                            )
                        if persisted is not None:
                            await self._repo.create_repo_event(EcosystemRepoEvent(
                                repo_id=persisted.id,
                                project_id=self._project_id or None,
                                event_type="discovered",
                                payload_json={
                                    "first_topics": repo_data.get("topics") or [],
                                    "first_stars": repo_data.get("stars", 0),
                                    "source_query": repo_matched_queries.get(fn, [""])[0],
                                },
                                source="scanner",
                                scan_run_id=scan_run.id,
                            ))
                    except Exception as ev_exc:
                        errors.append(f"event:discovered {fn}: {ev_exc!s}")
                else:
                    updated_count += 1
                    # v1.6.0: detect metadata changes and write events
                    # v1.6.1 Phase 2: also track metadata_changed_count for scan run stats
                    try:
                        events_to_write: list[EcosystemRepoEvent] = []
                        metadata_changed = False

                        old_topics = set(existing.topics or [])
                        new_topics = set(repo_data.get("topics") or [])
                        if old_topics != new_topics:
                            metadata_changed = True
                            events_to_write.append(EcosystemRepoEvent(
                                repo_id=existing.id,
                                project_id=self._project_id or None,
                                event_type="topics_changed",
                                payload_json={
                                    "before": sorted(old_topics),
                                    "after": sorted(new_topics),
                                },
                                source="scanner",
                                scan_run_id=scan_run.id,
                            ))
                        old_stars = existing.stars or 0
                        new_stars = repo_data.get("stars", 0)
                        if old_stars > 0 and new_stars > 0:
                            pct_change = (new_stars - old_stars) / old_stars
                            if abs(pct_change) >= 0.10:
                                metadata_changed = True
                                events_to_write.append(EcosystemRepoEvent(
                                    repo_id=existing.id,
                                    project_id=self._project_id or None,
                                    event_type="stars_jumped",
                                    payload_json={
                                        "before": old_stars,
                                        "after": new_stars,
                                        "pct": round(pct_change * 100, 1),
                                    },
                                    source="scanner",
                                    scan_run_id=scan_run.id,
                                ))
                        # check description and language changes
                        if (existing.description or "") != (repo_data.get("description") or ""):
                            metadata_changed = True
                        if (existing.language or "") != (repo_data.get("language") or ""):
                            metadata_changed = True

                        if metadata_changed:
                            metadata_changed_count += 1
                        if events_to_write:
                            await self._repo.bulk_create_repo_events(events_to_write)
                    except Exception as ev_exc:
                        errors.append(f"event:update {fn}: {ev_exc!s}")
            except Exception as exc:
                errors.append(f"persist {repo_data.get('repo_full_name', '?')}: {exc!s}")

        elapsed = round(time.time() - start, 2)

        cat_dist: dict[str, int] = {}
        for r in all_repos.values():
            cat = r.get("relevance_category") or "unknown"
            cat_dist[cat] = cat_dist.get(cat, 0) + 1

        await self._repo.update_scan_run(
            scan_run.id,
            completed_at=datetime.now(tz=UTC),
            duration_seconds=elapsed,
            repos_added=new_count,
            repos_updated=updated_count,
            repos_skipped=skipped,
            metadata_changed_count=metadata_changed_count,
            errors=errors,
            notes=notes
            or f"strategy={strategy.value} queries={len(active_queries)} archived={archived_marked}",
        )

        return ScanResult(
            run_id=scan_run.id,
            strategy=strategy.value,
            scanned=len(all_repos),
            new_profiles=new_count,
            updated_profiles=updated_count,
            skipped=skipped,
            archived_marked=archived_marked,
            errors=errors,
            duration_seconds=elapsed,
            per_query_stats=per_query_stats,
            category_distribution=cat_dist,
        )

    # --------------------------------------------------------
    # Internal helpers
    # --------------------------------------------------------

    def _passes_filters(self, repo: dict[str, Any]) -> bool:
        """Apply secondary filters: stars, owner blacklist, description whitelist."""
        if repo.get("stars", 0) < self._config.min_stars:
            return False
        if _is_blacklisted_owner(repo, self._config.owner_blacklist):
            return False
        if self._config.require_description_keyword and not _matches_keyword_whitelist(
            repo, self._config.description_keywords
        ):
            return False
        return True

    def _is_within_refresh_window(self, last_scanned_at: datetime | None) -> bool:
        """Return True if last_scanned_at falls inside refresh_window_days."""
        if last_scanned_at is None:
            return False
        cutoff = datetime.now(tz=UTC) - timedelta(
            days=self._config.refresh_window_days
        )
        compare = (
            last_scanned_at
            if last_scanned_at.tzinfo
            else last_scanned_at.replace(tzinfo=UTC)
        )
        return compare >= cutoff

    def _build_profile(
        self,
        repo_data: dict[str, Any],
        scan_run_id: str,
        is_archived: bool,
        existing: EcosystemRepoProfile | None,
    ) -> EcosystemRepoProfile:
        """Compose an EcosystemRepoProfile from search hit + scan metadata."""
        now = datetime.now(tz=UTC)
        first_seen_at = existing.first_seen_at if existing else now
        description = repo_data.get("description")
        excerpt = (description or "")[:280]

        repo_full_name = repo_data["repo_full_name"]
        kwargs: dict[str, Any] = {}
        if existing is not None:
            kwargs["id"] = existing.id
        return EcosystemRepoProfile(
            **kwargs,
            repo_full_name=repo_full_name,
            name=repo_data.get("name", ""),
            owner=repo_data.get("owner", ""),
            description=description,
            stars=repo_data.get("stars", 0),
            language=repo_data.get("language"),
            topics=repo_data.get("topics") or [],
            homepage=repo_data.get("homepage"),
            last_commit_at=repo_data.get("last_commit_at"),
            pushed_at=repo_data.get("pushed_at"),
            is_archived=is_archived,
            scan_run_id=scan_run_id,
            description_excerpt=excerpt,
            needs_deep_review=repo_data.get("needs_deep_review", repo_data.get("stars", 0) < 15000),
            relevance_category=repo_data.get("relevance_category"),
            relevance_score=repo_data.get("relevance_score", 0),
            one_line_summary=repo_data.get("one_line_summary") or excerpt[:200] or None,
            first_seen_at=first_seen_at,
            last_scanned_at=now,
            canonical_id=existing.canonical_id if existing and existing.canonical_id else f"github/{repo_full_name}",
            source_kind=existing.source_kind if existing and existing.source_kind else "github",
        )


# ============================================================
# Default subprocess-backed gh search adapter
# ============================================================


async def default_gh_search(
    keyword: str,
    min_stars: int,
    topics: list[str] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Async wrapper around gh CLI search returning normalised repo dicts.

    Runs the blocking subprocess call in a default executor so callers stay async.
    Errors are converted into empty result + propagated logger.warning so the
    caller's error_collector still records the issue via try/except.

    Args:
        limit: Max results per query (default 500, gh max 1000).
               Higher limit = more candidates but slower per-repo topic fetch.
    """
    from aiteam.mcp.tools.ecosystem import _parse_gh_repo, _run_gh_search

    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(
        None,
        lambda: _run_gh_search(keyword, min_stars=min_stars, topics=topics, limit=limit),
    )

    result: list[dict[str, Any]] = []
    for item in items:
        parsed = _parse_gh_repo(item, min_stars, hint_topics=topics or [])
        if parsed is None:
            continue
        # Normalise datetime fields back to objects for the scanner.
        last_commit_at = parsed.get("last_commit_at")
        if isinstance(last_commit_at, str):
            try:
                parsed["last_commit_at"] = datetime.fromisoformat(
                    last_commit_at.replace("Z", "+00:00")
                )
            except ValueError:
                parsed["last_commit_at"] = None
        # gh returns pushedAt; mirror into pushed_at for archive detection.
        pushed_raw = item.get("pushedAt")
        if pushed_raw:
            try:
                parsed["pushed_at"] = datetime.fromisoformat(pushed_raw.replace("Z", "+00:00"))
            except ValueError:
                parsed["pushed_at"] = None
        else:
            parsed["pushed_at"] = parsed.get("last_commit_at")
        result.append(parsed)
    return result
