"""Stale-claim reclaim regression tests — Stage 0 queue deadlock (2026-07-27).

Production incident reproduced here: 74 ``ecosystem_deep_reviews`` rows sat at
``stage_status='queued' AND claimed_by='tick:<id>'`` for 17 days.  The row is
reserved at INSERT time (D5, so ``tick`` dispatch and the pull-based
``claim_next_shallow_repo`` can never grab the same row), but the reservation
had **no expiry** — once the dispatched sub-agent died without advancing the
stage, the row was invisible to every claimer forever and the queue reported
``pending=0`` while nothing progressed.

Invariants pinned here:

* a **fresh** reservation still blocks a rival claimer (D5 kept intact);
* a reservation older than :data:`STALE_CLAIM_TTL_SECONDS` is reclaimable;
* a row with ``claimed_by`` set but ``claimed_at`` NULL counts as stale
  (unknown age == unrecoverable otherwise);
* ``reclaim_stale_shallow_claims`` previews without writing when
  ``dry_run=True`` and only touches stale rows when applied.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import STALE_CLAIM_TTL_SECONDS, StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemRepoProfile,
    EcosystemStageStatus,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repo for isolation."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


def _utc_naive(delta: timedelta | None = None) -> datetime:
    """Naive-UTC timestamp, matching how the ORM persists ``claimed_at``."""
    now = utc_now().replace(tzinfo=None)
    return now + delta if delta else now


async def _make_profile(repo: StorageRepository, full_name: str) -> str:
    profile = EcosystemRepoProfile(
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=9000,
        last_scanned_at=utc_now(),
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile(full_name)
    assert fetched is not None
    return fetched.id


async def _make_tick_reserved_review(
    repo: StorageRepository,
    repo_id: str,
    *,
    age: timedelta | None = None,
) -> EcosystemDeepReview:
    """Recreate a ``tick``-dispatched row exactly as ``_dispatch_one`` writes it."""
    review = EcosystemDeepReview(
        repo_id=repo_id,
        stage_status=EcosystemStageStatus.QUEUED,
        claimed_at=_utc_naive(),
    )
    review.claimed_by = f"tick:{review.id[:8]}"
    created = await repo.create_deep_review(review)
    if age is not None:
        await repo.update_deep_review(created.id, claimed_at=_utc_naive(-age))
    return created


# ---------------------------------------------------------------------------
# Deadlock reproduction
# ---------------------------------------------------------------------------


async def test_fresh_tick_reservation_still_blocks_rival_claimer(
    repo: StorageRepository,
) -> None:
    """D5 intact: a just-dispatched row is never double-claimed."""
    rid = await _make_profile(repo, "owner/fresh-reserved")
    await _make_tick_reserved_review(repo, rid)

    assert await repo.claim_next_shallow_repo(worker_id="rival") is None


async def test_stale_tick_reservation_is_reclaimable(
    repo: StorageRepository,
) -> None:
    """The 17-day deadlock: an abandoned reservation must expire, not wedge."""
    rid = await _make_profile(repo, "owner/stale-reserved")
    created = await _make_tick_reserved_review(repo, rid, age=timedelta(days=17))

    claimed = await repo.claim_next_shallow_repo(worker_id="rescue-worker")

    assert claimed is not None, "stale claim must be reclaimable (queue deadlock)"
    assert claimed.id == created.id
    assert claimed.claimed_by == "rescue-worker"


async def test_reservation_without_timestamp_is_stale(
    repo: StorageRepository,
) -> None:
    """``claimed_by`` set with no ``claimed_at`` has unknowable age → reclaimable."""
    rid = await _make_profile(repo, "owner/no-timestamp")
    created = await _make_tick_reserved_review(repo, rid)
    await repo.update_deep_review(created.id, claimed_at=None)

    claimed = await repo.claim_next_shallow_repo(worker_id="rescue-worker")

    assert claimed is not None
    assert claimed.id == created.id


async def test_ttl_boundary_just_inside_is_protected(
    repo: StorageRepository,
) -> None:
    """A reservation younger than the TTL keeps its exclusive hold."""
    rid = await _make_profile(repo, "owner/ttl-boundary")
    await _make_tick_reserved_review(
        repo, rid, age=timedelta(seconds=STALE_CLAIM_TTL_SECONDS - 120)
    )

    assert await repo.claim_next_shallow_repo(worker_id="rival") is None


# ---------------------------------------------------------------------------
# Bulk reclaim helper (backs scripts/reclaim_stale_shallow_claims.py)
# ---------------------------------------------------------------------------


async def test_reclaim_dry_run_lists_without_writing(
    repo: StorageRepository,
) -> None:
    stale_ids = []
    for i in range(2):
        rid = await _make_profile(repo, f"owner/stale-{i}")
        created = await _make_tick_reserved_review(repo, rid, age=timedelta(days=17))
        stale_ids.append(created.id)
    fresh_rid = await _make_profile(repo, "owner/fresh")
    fresh = await _make_tick_reserved_review(repo, fresh_rid)

    preview = await repo.reclaim_stale_shallow_claims(dry_run=True)

    assert {row["id"] for row in preview} == set(stale_ids)
    assert all(row["claimed_by"].startswith("tick:") for row in preview)
    assert all(row["stale_seconds"] > STALE_CLAIM_TTL_SECONDS for row in preview)

    # Nothing written: rows still hold their reservation.
    for dr_id in stale_ids:
        row = await repo.get_deep_review(dr_id)
        assert row is not None and row.claimed_by is not None
    untouched = await repo.get_deep_review(fresh.id)
    assert untouched is not None and untouched.claimed_by is not None


async def test_reclaim_apply_releases_only_stale_rows(
    repo: StorageRepository,
) -> None:
    rid_stale = await _make_profile(repo, "owner/apply-stale")
    stale = await _make_tick_reserved_review(repo, rid_stale, age=timedelta(days=17))
    rid_fresh = await _make_profile(repo, "owner/apply-fresh")
    fresh = await _make_tick_reserved_review(repo, rid_fresh)

    released = await repo.reclaim_stale_shallow_claims(dry_run=False)

    assert [row["id"] for row in released] == [stale.id]

    reopened = await repo.get_deep_review(stale.id)
    assert reopened is not None
    assert reopened.claimed_by is None
    assert reopened.claimed_at is None
    assert reopened.stage_status == EcosystemStageStatus.QUEUED

    kept = await repo.get_deep_review(fresh.id)
    assert kept is not None and kept.claimed_by is not None


async def test_reclaim_custom_ttl_argument(repo: StorageRepository) -> None:
    """Callers (the ops script) can widen/narrow the TTL for a one-off sweep."""
    rid = await _make_profile(repo, "owner/custom-ttl")
    created = await _make_tick_reserved_review(repo, rid, age=timedelta(minutes=5))

    assert await repo.reclaim_stale_shallow_claims(dry_run=True) == []

    preview = await repo.reclaim_stale_shallow_claims(dry_run=True, ttl_seconds=60)
    assert [row["id"] for row in preview] == [created.id]
