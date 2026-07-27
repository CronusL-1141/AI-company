"""Integration tests — shallow batch approval must produce runnable dispatches.

Regression target (2026-07-27 audit): approval used to hand-roll bare
``EcosystemDeepReview`` inserts (no ``dispatch_prompt``), then fire
``worker.tick()`` and throw the result away behind ``except Exception: pass``.
Two consequences:

* nothing downstream could ever run the approved repos — the rows carried no
  prompt, and ``tick`` skipped those very repos because they were already
  ``queued``;
* the caller got no dispatch instructions back, so the approval was a dead end.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiteam.api.app import create_app
from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoProfile

PROJECT_ID = "proj-batch-approve-001"
HEADERS = {"X-Project-Id": PROJECT_ID}


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://", project_scope=PROJECT_ID)
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def client(repo: StorageRepository) -> AsyncClient:
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_scoped_repository] = lambda: repo
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac  # type: ignore[misc]


async def _seed_profile(repo: StorageRepository, full_name: str) -> str:
    profile = EcosystemRepoProfile(
        project_id=PROJECT_ID,
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=8000,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile(full_name, project_id=PROJECT_ID)
    assert fetched is not None
    return fetched.id


async def _create_batch(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/ecosystem/shallow_batches",
        json={"triggered_by": "tester", "trigger_reason": "unit"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    return resp.json()["batch_id"]


@pytest.mark.asyncio
async def test_approve_returns_runnable_dispatch_intents(
    client: AsyncClient,
    repo: StorageRepository,
) -> None:
    await _seed_profile(repo, "owner/alpha")
    await _seed_profile(repo, "owner/beta")
    batch_id = await _create_batch(client)

    resp = await client.post(
        f"/api/ecosystem/shallow_batches/{batch_id}/approve",
        json={"approved_by": "leader"},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["status"] == "running"
    assert body["dr_created"] == 2
    assert len(body["intents"]) == 2

    for intent in body["intents"]:
        assert intent["deep_review_id"]
        assert intent["repo_full_name"].startswith("owner/")
        # The prompt is the whole point — an empty one means nothing can run.
        assert "浅扫" in intent["prompt"]
        assert intent["deep_review_id"] in intent["prompt"]

        row = await repo.get_deep_review(intent["deep_review_id"])
        assert row is not None
        assert row.dispatch_prompt
        assert row.batch_id == batch_id


@pytest.mark.asyncio
async def test_approve_skips_repos_already_in_flight(
    client: AsyncClient,
    repo: StorageRepository,
) -> None:
    await _seed_profile(repo, "owner/gamma")
    batch_id = await _create_batch(client)

    first = await client.post(
        f"/api/ecosystem/shallow_batches/{batch_id}/approve",
        json={"approved_by": "leader"},
        headers=HEADERS,
    )
    assert first.json()["dr_created"] == 1

    second_batch = await _create_batch(client)
    second = await client.post(
        f"/api/ecosystem/shallow_batches/{second_batch}/approve",
        json={"approved_by": "leader"},
        headers=HEADERS,
    )
    body = second.json()
    assert body["dr_created"] == 0
    assert body["skipped_count"] == 1
    assert body["intents"] == []


@pytest.mark.asyncio
async def test_approve_rejects_corrupt_snapshot(
    client: AsyncClient,
    repo: StorageRepository,
) -> None:
    """A corrupt snapshot must fail loudly instead of silently approving nothing."""
    await _seed_profile(repo, "owner/delta")
    batch_id = await _create_batch(client)
    await repo.update_shallow_batch(batch_id, candidates_snapshot_json="{not json")

    resp = await client.post(
        f"/api/ecosystem/shallow_batches/{batch_id}/approve",
        json={"approved_by": "leader"},
        headers=HEADERS,
    )

    assert resp.status_code == 500
    batch = await repo.get_shallow_batch(batch_id)
    assert batch is not None
    assert batch.status == "pending_approval"  # not advanced


@pytest.mark.asyncio
async def test_approve_twice_is_rejected(
    client: AsyncClient,
    repo: StorageRepository,
) -> None:
    await _seed_profile(repo, "owner/epsilon")
    batch_id = await _create_batch(client)

    ok = await client.post(
        f"/api/ecosystem/shallow_batches/{batch_id}/approve",
        json={"approved_by": "leader"},
        headers=HEADERS,
    )
    assert ok.status_code == 200

    again = await client.post(
        f"/api/ecosystem/shallow_batches/{batch_id}/approve",
        json={"approved_by": "leader"},
        headers=HEADERS,
    )
    assert again.status_code == 400
