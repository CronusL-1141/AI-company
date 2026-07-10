"""Unit tests for v1.5.0-B Stage 0 resurrection mechanism (§3.3).

Once a profile is flagged ``is_deleted`` or ``is_private_now`` the
periodic scanner / cron periodically re-checks it. If GitHub responds
again with ``200`` we clear the failure flags and restore activity.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest_asyncio

from aiteam.services.ecosystem_shallow_queue import EcosystemShallowQueueWorker
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoProfile


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _seed(
    repo: StorageRepository,
    full_name: str = "owner/r",
    *,
    project_id: str = "proj-revive",
) -> str:
    profile = EcosystemRepoProfile(
        project_id=project_id,
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=2000,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile, project_id=project_id)
    fetched = await repo.get_ecosystem_profile(full_name, project_id=project_id)
    assert fetched is not None
    return fetched.id


def _gh_returns(http_status: int):
    async def _fetcher(repo_full_name: str) -> dict[str, Any]:
        return {"http_status": http_status, "repo_full_name": repo_full_name}

    return _fetcher


# ============================================================
# Tests
# ============================================================


async def test_revive_clears_deleted_flag_when_repo_alive_again(
    repo: StorageRepository,
) -> None:
    rid = await _seed(repo, "owner/back")
    await repo.mark_profile_deleted(rid, project_id="proj-revive")

    worker = EcosystemShallowQueueWorker(
        repo,
        project_id="proj-revive",
        gh_fetcher=_gh_returns(200),
    )
    revived = await worker.revive_check_one(rid)
    assert revived is True

    profile = await repo.get_ecosystem_profile_by_id(
        rid, project_id="proj-revive"
    )
    assert profile is not None
    assert profile.is_deleted is False
    assert profile.is_active is True
    assert profile.fetch_failure_count == 0


async def test_revive_clears_private_flag_when_repo_public_again(
    repo: StorageRepository,
) -> None:
    rid = await _seed(repo, "owner/public")
    await repo.mark_profile_private(rid, project_id="proj-revive")

    worker = EcosystemShallowQueueWorker(
        repo,
        project_id="proj-revive",
        gh_fetcher=_gh_returns(200),
    )
    revived = await worker.revive_check_one(rid)
    assert revived is True

    profile = await repo.get_ecosystem_profile_by_id(
        rid, project_id="proj-revive"
    )
    assert profile.is_private_now is False
    assert profile.is_active is True


async def test_revive_skips_non_failed_repos(repo: StorageRepository) -> None:
    """Profiles that aren't deleted/private don't get revived."""
    rid = await _seed(repo, "owner/healthy")

    worker = EcosystemShallowQueueWorker(
        repo,
        project_id="proj-revive",
        gh_fetcher=_gh_returns(200),
    )
    revived = await worker.revive_check_one(rid)
    assert revived is False


async def test_revive_no_op_when_still_404(repo: StorageRepository) -> None:
    """Still-deleted repos remain flagged."""
    rid = await _seed(repo, "owner/gone")
    await repo.mark_profile_deleted(rid, project_id="proj-revive")

    worker = EcosystemShallowQueueWorker(
        repo,
        project_id="proj-revive",
        gh_fetcher=_gh_returns(404),
    )
    revived = await worker.revive_check_one(rid)
    assert revived is False

    profile = await repo.get_ecosystem_profile_by_id(
        rid, project_id="proj-revive"
    )
    assert profile is not None
    assert profile.is_deleted is True


async def test_revive_no_op_when_no_fetcher_injected(
    repo: StorageRepository,
) -> None:
    """Missing gh_fetcher means no revival possible (safe default)."""
    rid = await _seed(repo, "owner/mute")
    await repo.mark_profile_deleted(rid, project_id="proj-revive")

    worker = EcosystemShallowQueueWorker(
        repo,
        project_id="proj-revive",
        gh_fetcher=None,
    )
    revived = await worker.revive_check_one(rid)
    assert revived is False
