"""Unit tests for v1.5.0-D EcosystemRefresher service.

Covers:
* shallow_refresh: active-set scoping, diff-based skip, snapshot append,
  failed-flag handling (404 / 403 / rate-limit / 5xx).
* recompute_active_set: promotion + demotion + auto-queue Stage 0 on
  promotion of repos lacking shallow_summary.
* resurrect: revival + lifecycle tag removal + Stage 0 re-queue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio

from aiteam.services.ecosystem_lifecycle import (
    LIFECYCLE_TAG_DELETED,
    LIFECYCLE_TAG_PRIVATE_NOW,
)
from aiteam.services.ecosystem_refresher import (
    WEEKLY_REFRESH_CRON_NAME,
    WEEKLY_REFRESH_EVENT_TYPE,
    EcosystemRefresher,
    _coerce_datetime,
    _has_new_push,
    build_weekly_refresh_cron_payload,
)
from aiteam.services.ecosystem_shallow_queue import EcosystemShallowQueueWorker
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemProjectSettings,
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)

PROJECT = "proj-refresher"


# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


# ============================================================
# Helpers
# ============================================================


async def _seed_settings(
    repo: StorageRepository,
    *,
    project_id: str = PROJECT,
    min_stars: int = 1000,
    top_n: int = 5,
) -> None:
    settings = EcosystemProjectSettings(
        project_id=project_id,
        min_stars=min_stars,
        top_n=top_n,
    )
    await repo.upsert_ecosystem_project_settings(settings)


async def _seed_profile(
    repo: StorageRepository,
    full_name: str,
    *,
    stars: int,
    project_id: str = PROJECT,
    is_active: bool = True,
    shallow_summary: str = "",
    last_refreshed_days_ago: int | None = None,
    is_deleted: bool = False,
    is_private_now: bool = False,
) -> str:
    last_ref = None
    if last_refreshed_days_ago is not None:
        last_ref = datetime.now(tz=UTC) - timedelta(
            days=last_refreshed_days_ago
        )
    profile = EcosystemRepoProfile(
        project_id=project_id,
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=stars,
        is_active=is_active,
        shallow_summary=shallow_summary,
        last_shallow_refreshed_at=last_ref,
        is_deleted=is_deleted,
        is_private_now=is_private_now,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile, project_id=project_id)
    fetched = await repo.get_ecosystem_profile(full_name, project_id=project_id)
    assert fetched is not None
    return fetched.id


def _make_fetcher(responses: dict[str, dict[str, Any]]):
    """Build a gh_fetcher returning the mapped response per repo_full_name."""

    async def _fetcher(repo_full_name: str) -> dict[str, Any]:
        if repo_full_name not in responses:
            return {"http_status": 200, "stars": 1000, "pushed_at": None}
        return responses[repo_full_name]

    return _fetcher


# ============================================================
# Pure helpers
# ============================================================


def test_coerce_datetime_handles_iso_z_string() -> None:
    out = _coerce_datetime("2026-01-01T00:00:00Z")
    assert out is not None
    assert out.tzinfo is not None
    assert out.year == 2026


def test_coerce_datetime_handles_naive_datetime() -> None:
    naive = datetime(2026, 5, 1, 12, 0)
    out = _coerce_datetime(naive)
    assert out is not None
    assert out.tzinfo is UTC


def test_build_weekly_refresh_cron_payload_emits_canonical_event() -> None:
    payload = build_weekly_refresh_cron_payload(project_id="proj-x")
    assert payload["name"] == WEEKLY_REFRESH_CRON_NAME
    assert payload["action_type"] == "emit_event"
    assert payload["interval"] == "7 days"
    assert WEEKLY_REFRESH_EVENT_TYPE in payload["action_config"]
    assert "proj-x" in payload["action_config"]
    assert "proj-x" in payload["description"]


def test_has_new_push_first_time_when_no_summary_yet() -> None:
    profile = EcosystemRepoProfile(
        repo_full_name="o/r",
        name="r",
        owner="o",
        shallow_summary="",
    )
    assert (
        _has_new_push(profile, datetime.now(tz=UTC)) is True
    )


def test_has_new_push_skips_when_pushed_at_older_than_last_refresh() -> None:
    last = datetime(2026, 5, 1, tzinfo=UTC)
    profile = EcosystemRepoProfile(
        repo_full_name="o/r",
        name="r",
        owner="o",
        shallow_summary="had summary",
        last_shallow_refreshed_at=last,
    )
    earlier = datetime(2026, 4, 30, tzinfo=UTC)
    assert _has_new_push(profile, earlier) is False


def test_has_new_push_detects_newer_push() -> None:
    last = datetime(2026, 5, 1, tzinfo=UTC)
    profile = EcosystemRepoProfile(
        repo_full_name="o/r",
        name="r",
        owner="o",
        shallow_summary="had summary",
        last_shallow_refreshed_at=last,
    )
    later = datetime(2026, 5, 5, tzinfo=UTC)
    assert _has_new_push(profile, later) is True


# ============================================================
# shallow_refresh
# ============================================================


async def test_shallow_refresh_refreshes_active_repos_with_new_push(
    repo: StorageRepository,
) -> None:
    """Active repo with newer pushed_at gets re-queued via worker."""
    await _seed_settings(repo, top_n=3)
    rid_a = await _seed_profile(
        repo,
        "owner/a",
        stars=10000,
        shallow_summary="既有总结",
        last_refreshed_days_ago=30,
    )
    # Sleeper repo: pushed_at older than last refresh → skipped.
    rid_b = await _seed_profile(
        repo,
        "owner/b",
        stars=8000,
        shallow_summary="既有总结",
        last_refreshed_days_ago=2,
    )

    fetcher = _make_fetcher(
        {
            "owner/a": {
                "http_status": 200,
                "stars": 10500,
                "pushed_at": datetime.now(tz=UTC).isoformat(),
            },
            "owner/b": {
                "http_status": 200,
                "stars": 8000,
                "pushed_at": (
                    datetime.now(tz=UTC) - timedelta(days=10)
                ).isoformat(),
            },
        }
    )
    worker = EcosystemShallowQueueWorker(repo, project_id=PROJECT)
    refresher = EcosystemRefresher(
        repo, worker, gh_fetcher=fetcher, project_id=PROJECT
    )

    result = await refresher.shallow_refresh()

    assert result.active_total == 2
    assert result.refreshed == 1  # only owner/a — owner/b had no new push
    assert result.skipped_no_diff == 1
    assert result.snapshots_written == 2  # snapshot regardless of diff
    assert result.marked_deleted == 0
    assert result.marked_private == 0
    assert len(result.queued_intents) == 1
    intent = result.queued_intents[0]
    assert intent.repo_id == rid_a
    assert intent.repo_full_name == "owner/a"

    # Owner/b stayed in same state — no Stage 0 review created beyond owner/a.
    snapshots_a = await repo.list_status_snapshots(
        repo_id=rid_a, project_id=PROJECT
    )
    snapshots_b = await repo.list_status_snapshots(
        repo_id=rid_b, project_id=PROJECT
    )
    assert len(snapshots_a) == 1
    assert len(snapshots_b) == 1
    assert snapshots_a[0].stars == 10500


async def test_shallow_refresh_marks_deleted_on_404(
    repo: StorageRepository,
) -> None:
    """404 from GitHub flips the profile into is_deleted + is_active=False."""
    await _seed_settings(repo)
    rid = await _seed_profile(
        repo, "owner/gone", stars=5000, shallow_summary="x"
    )

    fetcher = _make_fetcher(
        {"owner/gone": {"http_status": 404, "error_message": "Not Found"}}
    )
    worker = EcosystemShallowQueueWorker(repo, project_id=PROJECT)
    refresher = EcosystemRefresher(
        repo, worker, gh_fetcher=fetcher, project_id=PROJECT
    )
    result = await refresher.shallow_refresh()

    assert result.marked_deleted == 1
    assert result.refreshed == 0

    profile = await repo.get_ecosystem_profile_by_id(rid, project_id=PROJECT)
    assert profile is not None
    assert profile.is_deleted is True
    assert profile.is_active is False


async def test_shallow_refresh_marks_private_on_403(
    repo: StorageRepository,
) -> None:
    """403 (no rate-limit hint) flips into is_private_now=True."""
    await _seed_settings(repo)
    rid = await _seed_profile(
        repo, "owner/secret", stars=5000, shallow_summary="x"
    )

    fetcher = _make_fetcher(
        {
            "owner/secret": {
                "http_status": 403,
                "rate_limit_remaining": 100,  # not rate-limit
                "error_message": "Forbidden",
            }
        }
    )
    worker = EcosystemShallowQueueWorker(repo, project_id=PROJECT)
    refresher = EcosystemRefresher(
        repo, worker, gh_fetcher=fetcher, project_id=PROJECT
    )
    result = await refresher.shallow_refresh()
    assert result.marked_private == 1

    profile = await repo.get_ecosystem_profile_by_id(rid, project_id=PROJECT)
    assert profile is not None
    assert profile.is_private_now is True
    assert profile.is_active is False


async def test_shallow_refresh_treats_403_rate_limit_as_transient(
    repo: StorageRepository,
) -> None:
    """403 with rate_limit_remaining=0 stays as a transient error."""
    await _seed_settings(repo)
    rid = await _seed_profile(repo, "owner/limited", stars=5000)

    fetcher = _make_fetcher(
        {
            "owner/limited": {
                "http_status": 403,
                "rate_limit_remaining": 0,
                "error_message": "rate limit exceeded",
            }
        }
    )
    worker = EcosystemShallowQueueWorker(repo, project_id=PROJECT)
    refresher = EcosystemRefresher(
        repo, worker, gh_fetcher=fetcher, project_id=PROJECT
    )
    result = await refresher.shallow_refresh()

    assert result.transient_errors == 1
    assert result.marked_private == 0
    assert result.marked_deleted == 0

    profile = await repo.get_ecosystem_profile_by_id(rid, project_id=PROJECT)
    assert profile is not None
    assert profile.is_private_now is False
    assert profile.fetch_failure_count == 1


async def test_shallow_refresh_only_scans_active_set(
    repo: StorageRepository,
) -> None:
    """Repos outside top_n are not touched at all."""
    await _seed_settings(repo, top_n=2)
    high_a = await _seed_profile(repo, "owner/high1", stars=20_000)
    high_b = await _seed_profile(repo, "owner/high2", stars=15_000)
    # below top_n cutoff — should not appear in refresh
    low = await _seed_profile(repo, "owner/low", stars=2_000, is_active=False)

    pushed_iso = datetime.now(tz=UTC).isoformat()
    fetcher = _make_fetcher(
        {
            "owner/high1": {
                "http_status": 200,
                "stars": 20_001,
                "pushed_at": pushed_iso,
            },
            "owner/high2": {
                "http_status": 200,
                "stars": 15_001,
                "pushed_at": pushed_iso,
            },
        }
    )
    worker = EcosystemShallowQueueWorker(repo, project_id=PROJECT)
    refresher = EcosystemRefresher(
        repo, worker, gh_fetcher=fetcher, project_id=PROJECT
    )
    result = await refresher.shallow_refresh()

    assert result.active_total == 2
    fetched_full_names = {i.repo_full_name for i in result.queued_intents}
    assert fetched_full_names == {"owner/high1", "owner/high2"}

    # The low profile had no GitHub call → no snapshot row.
    assert (
        await repo.list_status_snapshots(repo_id=low, project_id=PROJECT)
        == []
    )
    assert (
        await repo.list_status_snapshots(repo_id=high_a, project_id=PROJECT)
        != []
    )
    assert (
        await repo.list_status_snapshots(repo_id=high_b, project_id=PROJECT)
        != []
    )


async def test_shallow_refresh_skips_when_no_gh_fetcher(
    repo: StorageRepository,
) -> None:
    """Missing gh_fetcher records the batch but performs no work."""
    await _seed_settings(repo)
    await _seed_profile(repo, "owner/r", stars=5000)
    worker = EcosystemShallowQueueWorker(repo, project_id=PROJECT)
    refresher = EcosystemRefresher(
        repo, worker, gh_fetcher=None, project_id=PROJECT
    )
    result = await refresher.shallow_refresh()
    assert result.refreshed == 0
    assert any("gh_fetcher" in e for e in result.errors)


# ============================================================
# recompute_active_set
# ============================================================


async def test_recompute_active_set_promotes_climber_and_queues_stage0(
    repo: StorageRepository,
) -> None:
    """Profile that climbs into top_n gets is_active=True and is queued."""
    await _seed_settings(repo, top_n=2)
    # winners
    a = await _seed_profile(repo, "owner/win1", stars=20_000)
    b = await _seed_profile(repo, "owner/win2", stars=15_000)
    # climber starts inactive without summary
    climber = await _seed_profile(
        repo,
        "owner/climber",
        stars=18_000,
        is_active=False,
        shallow_summary="",
    )

    # Top_n=2 with stars desc: win1 (20k), climber (18k). win2 should be demoted.
    worker = EcosystemShallowQueueWorker(repo, project_id=PROJECT)
    refresher = EcosystemRefresher(
        repo, worker, project_id=PROJECT
    )
    outcome = await refresher.recompute_active_set()

    assert outcome.active_total == 2
    assert outcome.promoted == 1
    assert outcome.demoted == 1
    # climber promoted → queue_for_shallow returned an intent
    assert outcome.queued_for_shallow == 1
    assert outcome.queued_intents[0].repo_id == climber

    refreshed = await repo.get_ecosystem_profile_by_id(
        climber, project_id=PROJECT
    )
    assert refreshed is not None
    assert refreshed.is_active is True
    assert refreshed.active_rank == 2

    demoted = await repo.get_ecosystem_profile_by_id(b, project_id=PROJECT)
    assert demoted is not None
    assert demoted.is_active is False

    # winner stays active
    winner = await repo.get_ecosystem_profile_by_id(a, project_id=PROJECT)
    assert winner is not None
    assert winner.is_active is True
    assert winner.active_rank == 1


async def test_recompute_active_set_skips_failed_repos(
    repo: StorageRepository,
) -> None:
    """Deleted/private repos are excluded from active-set ranking."""
    await _seed_settings(repo, top_n=2)
    await _seed_profile(repo, "owner/active", stars=5000)
    deleted_id = await _seed_profile(
        repo,
        "owner/dead",
        stars=99_999,  # would be top by stars but is_deleted
        is_deleted=True,
    )

    refresher = EcosystemRefresher(repo, project_id=PROJECT)
    outcome = await refresher.recompute_active_set()
    assert outcome.active_total == 1

    profile = await repo.get_ecosystem_profile_by_id(
        deleted_id, project_id=PROJECT
    )
    assert profile is not None
    assert profile.is_active is False
    assert profile.active_rank is None


async def test_recompute_does_not_requeue_when_summary_already_present(
    repo: StorageRepository,
) -> None:
    """Profile with an existing summary is promoted but not re-queued."""
    await _seed_settings(repo, top_n=2)
    await _seed_profile(repo, "owner/winner", stars=20_000)
    summarized = await _seed_profile(
        repo,
        "owner/back",
        stars=18_000,
        is_active=False,
        shallow_summary="既有总结",
    )

    worker = EcosystemShallowQueueWorker(repo, project_id=PROJECT)
    refresher = EcosystemRefresher(
        repo, worker, project_id=PROJECT
    )
    outcome = await refresher.recompute_active_set()

    assert outcome.promoted == 1
    assert outcome.queued_for_shallow == 0  # already summarized

    refreshed = await repo.get_ecosystem_profile_by_id(
        summarized, project_id=PROJECT
    )
    assert refreshed is not None
    assert refreshed.is_active is True


# ============================================================
# resurrect
# ============================================================


async def _seed_lifecycle_tag(
    repo: StorageRepository,
    name: str,
) -> EcosystemTag:
    tag = EcosystemTag(
        name=name,
        category=EcosystemTagCategory.POSITIONING,
        description=f"lifecycle:{name}",
    )
    await repo.upsert_tag(tag)
    seeded = await repo.get_tag_by_name(name)
    assert seeded is not None
    return seeded


def _gh_returns(http_status: int):
    async def _fetcher(repo_full_name: str) -> dict[str, Any]:
        return {"http_status": http_status, "repo_full_name": repo_full_name}

    return _fetcher


async def test_resurrect_revives_deleted_repo_and_clears_tag(
    repo: StorageRepository,
) -> None:
    """resurrect drops lifecycle:deleted tag and re-queues Stage 0."""
    await _seed_settings(repo)
    rid = await _seed_profile(
        repo, "owner/back", stars=5000, is_deleted=True
    )
    deleted_tag = await _seed_lifecycle_tag(repo, LIFECYCLE_TAG_DELETED)
    await repo.add_repo_tag(
        EcosystemRepoTag(
            project_id=PROJECT,
            repo_id=rid,
            tag_id=deleted_tag.id,
            source=EcosystemTagSource.LIFECYCLE,
        ),
        project_id=PROJECT,
    )

    worker = EcosystemShallowQueueWorker(
        repo, project_id=PROJECT, gh_fetcher=_gh_returns(200)
    )
    refresher = EcosystemRefresher(
        repo, worker, project_id=PROJECT
    )
    outcome = await refresher.resurrect(rid)

    assert outcome.revived is True
    assert outcome.queued is True
    assert outcome.intent is not None
    assert outcome.intent.repo_id == rid

    profile = await repo.get_ecosystem_profile_by_id(rid, project_id=PROJECT)
    assert profile is not None
    assert profile.is_deleted is False
    assert profile.is_active is True
    assert profile.fetch_failure_count == 0

    tags = await repo.list_repo_tags(repo_id=rid, project_id=PROJECT)
    assert all(t.tag_id != deleted_tag.id for t in tags)


async def test_resurrect_revives_private_repo_and_clears_tag(
    repo: StorageRepository,
) -> None:
    await _seed_settings(repo)
    rid = await _seed_profile(
        repo, "owner/public-again", stars=5000, is_private_now=True
    )
    private_tag = await _seed_lifecycle_tag(repo, LIFECYCLE_TAG_PRIVATE_NOW)
    await repo.add_repo_tag(
        EcosystemRepoTag(
            project_id=PROJECT,
            repo_id=rid,
            tag_id=private_tag.id,
            source=EcosystemTagSource.LIFECYCLE,
        ),
        project_id=PROJECT,
    )

    worker = EcosystemShallowQueueWorker(
        repo, project_id=PROJECT, gh_fetcher=_gh_returns(200)
    )
    refresher = EcosystemRefresher(repo, worker, project_id=PROJECT)
    outcome = await refresher.resurrect(rid)
    assert outcome.revived is True
    profile = await repo.get_ecosystem_profile_by_id(rid, project_id=PROJECT)
    assert profile is not None
    assert profile.is_private_now is False

    tags = await repo.list_repo_tags(repo_id=rid, project_id=PROJECT)
    assert all(t.tag_id != private_tag.id for t in tags)


async def test_resurrect_no_op_when_repo_still_404(
    repo: StorageRepository,
) -> None:
    """Still-deleted repos remain flagged and no intent is produced."""
    await _seed_settings(repo)
    rid = await _seed_profile(
        repo, "owner/dead", stars=5000, is_deleted=True
    )

    worker = EcosystemShallowQueueWorker(
        repo, project_id=PROJECT, gh_fetcher=_gh_returns(404)
    )
    refresher = EcosystemRefresher(repo, worker, project_id=PROJECT)
    outcome = await refresher.resurrect(rid)
    assert outcome.revived is False
    assert outcome.queued is False

    profile = await repo.get_ecosystem_profile_by_id(rid, project_id=PROJECT)
    assert profile is not None
    assert profile.is_deleted is True


async def test_resurrect_no_op_when_repo_not_failed(
    repo: StorageRepository,
) -> None:
    """Healthy repos do not trigger resurrection."""
    await _seed_settings(repo)
    rid = await _seed_profile(repo, "owner/healthy", stars=5000)

    worker = EcosystemShallowQueueWorker(
        repo, project_id=PROJECT, gh_fetcher=_gh_returns(200)
    )
    refresher = EcosystemRefresher(repo, worker, project_id=PROJECT)
    outcome = await refresher.resurrect(rid)
    assert outcome.revived is False
    assert "未处于" in outcome.note
