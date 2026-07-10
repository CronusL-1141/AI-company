"""Unit tests for the EcosystemScanner service (Stage C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from aiteam.services.ecosystem_scanner import (
    DEFAULT_QUERIES,
    EcosystemScanner,
    FilterConfig,
    _classify_archived,
    _is_blacklisted_owner,
    _matches_keyword_whitelist,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRepoProfile,
    EcosystemScanStrategy,
)

# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


def _make_repo_data(
    full_name: str = "anthropics/claude-code",
    stars: int = 30000,
    description: str = "Claude AI coding assistant with MCP support",
    owner: str = "anthropics",
    pushed_at: datetime | None = None,
    topics: list[str] | None = None,
) -> dict[str, Any]:
    """Build a parsed gh repo dict for tests."""
    return {
        "repo_full_name": full_name,
        "name": full_name.split("/")[-1],
        "owner": owner,
        "description": description,
        "stars": stars,
        "language": "Python",
        "topics": topics or ["claude-code", "mcp"],
        "homepage": None,
        "last_commit_at": pushed_at,
        "pushed_at": pushed_at,
        "needs_deep_review": stars < 15000,
        "relevance_category": "skill-system",
        "relevance_score": 8,
        "one_line_summary": description[:200],
    }


def _make_gh_search(items: list[dict[str, Any]]):
    """Return an async gh_search callable that ignores args and yields the items."""

    async def _gh_search(keyword: str, min_stars: int, topics: list[str] | None = None):
        return list(items)

    return _gh_search


# ============================================================
# Filter helpers
# ============================================================


class TestFilterHelpers:
    def test_keyword_whitelist_matches_description(self):
        repo = _make_repo_data(description="awesome agent framework")
        assert _matches_keyword_whitelist(repo, ["agent"]) is True

    def test_keyword_whitelist_rejects_unrelated(self):
        repo = {
            "description": "java tutorial guide",
            "name": "java-guide",
            "owner": "randuser",
            "topics": ["java", "tutorial"],
        }
        assert _matches_keyword_whitelist(repo, ["claude", "mcp", "anthropic"]) is False

    def test_keyword_whitelist_empty_keywords_passes(self):
        repo = _make_repo_data(description="no keywords here")
        assert _matches_keyword_whitelist(repo, []) is True

    def test_owner_blacklist_case_insensitive(self):
        repo = _make_repo_data(owner="Snailclimb")
        assert _is_blacklisted_owner(repo, ["snailclimb"]) is True

    def test_owner_blacklist_no_match(self):
        repo = _make_repo_data(owner="anthropics")
        assert _is_blacklisted_owner(repo, ["snailclimb", "evil"]) is False

    def test_classify_archived_old_push(self):
        old = datetime.now(tz=UTC) - timedelta(days=400)
        assert _classify_archived(old, threshold_days=365) is True

    def test_classify_archived_recent_push(self):
        recent = datetime.now(tz=UTC) - timedelta(days=10)
        assert _classify_archived(recent, threshold_days=365) is False

    def test_classify_archived_none(self):
        assert _classify_archived(None, threshold_days=365) is False


# ============================================================
# FilterConfig
# ============================================================


class TestFilterConfig:
    def test_default_config_has_owner_blacklist(self):
        cfg = FilterConfig()
        assert "Snailclimb" in cfg.owner_blacklist

    def test_default_config_includes_claude_keyword(self):
        cfg = FilterConfig()
        assert "claude" in cfg.description_keywords

    def test_from_env_overrides_min_stars(self, monkeypatch):
        monkeypatch.setenv("ECOSYSTEM_MIN_STARS", "2500")
        cfg = FilterConfig.from_env()
        assert cfg.min_stars == 2500

    def test_from_env_parses_blacklist(self, monkeypatch):
        monkeypatch.setenv("ECOSYSTEM_OWNER_BLACKLIST", "evil1, evil2 , evil3")
        cfg = FilterConfig.from_env()
        assert cfg.owner_blacklist == ["evil1", "evil2", "evil3"]

    def test_from_env_invalid_int_uses_default(self, monkeypatch):
        monkeypatch.setenv("ECOSYSTEM_MIN_STARS", "not-an-int")
        cfg = FilterConfig.from_env()
        assert cfg.min_stars == 1000  # default


# ============================================================
# Scanner end-to-end (mock gh_search)
# ============================================================


@pytest.mark.asyncio
async def test_scan_creates_run_and_persists_profiles(repo: StorageRepository):
    items = [_make_repo_data("anthropics/claude-code", stars=30000)]
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_make_gh_search(items),
        config=FilterConfig(min_stars=1000),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.INCREMENTAL,
        queries=(("", ("claude-code",)),),
    )
    assert result.scanned == 1
    assert result.new_profiles == 1
    assert result.errors == []

    fetched_run = await repo.get_scan_run(result.run_id)
    assert fetched_run is not None
    assert fetched_run.repos_added == 1
    assert fetched_run.completed_at is not None


@pytest.mark.asyncio
async def test_scan_skips_recently_scanned_when_incremental(repo: StorageRepository):
    """Incremental strategy must skip repos last scanned within refresh window."""
    full_name = "anthropics/claude-code"
    existing = EcosystemRepoProfile(
        repo_full_name=full_name,
        name="claude-code",
        owner="anthropics",
        stars=30000,
        description="Claude code",
        last_scanned_at=datetime.now(tz=UTC) - timedelta(days=1),
        first_seen_at=datetime.now(tz=UTC) - timedelta(days=30),
    )
    await repo.upsert_ecosystem_profile(existing)

    items = [_make_repo_data(full_name, stars=30000)]
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_make_gh_search(items),
        config=FilterConfig(min_stars=1000, refresh_window_days=7),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.INCREMENTAL,
        queries=(("", ("claude-code",)),),
    )
    assert result.skipped == 1
    assert result.new_profiles == 0
    assert result.updated_profiles == 0


@pytest.mark.asyncio
async def test_scan_full_strategy_rescans_recent_profiles(repo: StorageRepository):
    """Full strategy must NOT skip recently scanned repos."""
    full_name = "anthropics/claude-code"
    existing = EcosystemRepoProfile(
        repo_full_name=full_name,
        name="claude-code",
        owner="anthropics",
        stars=30000,
        last_scanned_at=datetime.now(tz=UTC) - timedelta(hours=1),
        first_seen_at=datetime.now(tz=UTC) - timedelta(days=30),
    )
    await repo.upsert_ecosystem_profile(existing)

    items = [_make_repo_data(full_name, stars=35000)]  # stars bumped
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_make_gh_search(items),
        config=FilterConfig(min_stars=1000),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.FULL,
        queries=(("", ("claude-code",)),),
    )
    assert result.skipped == 0
    assert result.updated_profiles == 1


@pytest.mark.asyncio
async def test_scan_marks_archived_when_pushed_old(repo: StorageRepository):
    old_pushed = datetime.now(tz=UTC) - timedelta(days=500)
    items = [_make_repo_data("dead/repo", stars=20000, pushed_at=old_pushed)]
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_make_gh_search(items),
        config=FilterConfig(min_stars=1000),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.INCREMENTAL,
        queries=(("", ("dead",)),),
    )
    assert result.archived_marked == 1
    persisted = await repo.get_ecosystem_profile("dead/repo")
    assert persisted is not None
    assert persisted.is_archived is True


@pytest.mark.asyncio
async def test_scan_filters_blacklisted_owner(repo: StorageRepository):
    items = [
        _make_repo_data("Snailclimb/JavaGuide", stars=120000, owner="Snailclimb"),
        _make_repo_data("anthropics/claude-code", stars=30000),
    ]
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_make_gh_search(items),
        config=FilterConfig(min_stars=1000, owner_blacklist=["Snailclimb"]),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.FULL,
        queries=(("", ("claude",)),),
    )
    assert result.scanned == 1
    persisted = await repo.get_ecosystem_profile("Snailclimb/JavaGuide")
    assert persisted is None


@pytest.mark.asyncio
async def test_scan_filters_no_keyword_match(repo: StorageRepository):
    items = [
        _make_repo_data(
            "random/awesome-list",
            stars=50000,
            description="awesome things",
            owner="random",
            topics=["awesome"],
        )
    ]
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_make_gh_search(items),
        config=FilterConfig(min_stars=1000, description_keywords=["claude", "mcp"]),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.FULL,
        queries=(("", ("test",)),),
    )
    assert result.scanned == 0
    assert result.new_profiles == 0


@pytest.mark.asyncio
async def test_scan_collects_gh_search_errors(repo: StorageRepository):
    """gh_search exceptions become entries in result.errors, not crashes."""

    async def _broken_gh(keyword: str, min_stars: int, topics: list[str] | None = None):
        raise ConnectionError("rate limit hit")

    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_broken_gh,
        config=FilterConfig(min_stars=1000),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.INCREMENTAL,
        queries=(("kw", ("topic-a",)),),
    )
    assert result.scanned == 0
    assert any("rate limit" in e for e in result.errors)
    fetched = await repo.get_scan_run(result.run_id)
    assert fetched is not None
    assert any("rate limit" in e for e in fetched.errors)


@pytest.mark.asyncio
async def test_scan_below_min_stars_filtered(repo: StorageRepository):
    items = [_make_repo_data("low/stars", stars=500)]
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_make_gh_search(items),
        config=FilterConfig(min_stars=1000),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.FULL,
        queries=(("", ("test",)),),
    )
    assert result.scanned == 0


@pytest.mark.asyncio
async def test_scan_records_per_query_stats(repo: StorageRepository):
    items = [_make_repo_data("anthropics/claude-code", stars=30000)]
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_make_gh_search(items),
        config=FilterConfig(min_stars=1000),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.INCREMENTAL,
        queries=(("a", ("b",)), ("c", ("d",))),
    )
    assert len(result.per_query_stats) == 2
    # First query nets 1, second sees same dup -> 0.
    values = list(result.per_query_stats.values())
    assert sum(values) == 1


@pytest.mark.asyncio
async def test_scan_default_queries_constant_present():
    """DEFAULT_QUERIES should expose the canonical 8-query plan."""
    assert len(DEFAULT_QUERIES) >= 6
    assert any("claude-code" in topics for _, topics in DEFAULT_QUERIES)


@pytest.mark.asyncio
async def test_scan_run_records_strategy_and_triggered_by(repo: StorageRepository):
    scanner = EcosystemScanner(
        repo=repo,
        gh_search=_make_gh_search([]),
        config=FilterConfig(min_stars=1000),
    )
    result = await scanner.scan(
        strategy=EcosystemScanStrategy.TRENDING,
        triggered_by="cron",
        queries=(("", ("trending",)),),
        notes="weekly cron",
    )
    fetched = await repo.get_scan_run(result.run_id)
    assert fetched is not None
    assert fetched.strategy == EcosystemScanStrategy.TRENDING
    assert fetched.triggered_by == "cron"
    assert fetched.notes == "weekly cron"
