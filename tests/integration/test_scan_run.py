"""Integration tests for the scan-runs REST endpoints (Stage C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiteam.api.app import create_app
from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def client(repo: StorageRepository) -> AsyncClient:
    """FastAPI test client with the repo dependency overridden."""
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_scoped_repository] = lambda: repo
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_create_scan_run_then_fetch(client: AsyncClient):
    resp = await client.post(
        "/api/ecosystem/scan-runs",
        json={"strategy": "incremental", "triggered_by": "manual", "notes": "test"},
    )
    assert resp.status_code == 200
    body = resp.json()
    run_id = body["id"]
    assert body["strategy"] == "incremental"
    assert body["completed_at"] is None

    fetch = await client.get(f"/api/ecosystem/scan-runs/{run_id}")
    assert fetch.status_code == 200
    assert fetch.json()["id"] == run_id


@pytest.mark.asyncio
async def test_get_scan_run_404(client: AsyncClient):
    resp = await client.get("/api/ecosystem/scan-runs/non-existent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_complete_scan_run_updates_stats(client: AsyncClient):
    create = await client.post(
        "/api/ecosystem/scan-runs",
        json={"strategy": "full", "triggered_by": "cron"},
    )
    run_id = create.json()["id"]

    update = await client.post(
        f"/api/ecosystem/scan-runs/{run_id}/complete",
        json={
            "duration_seconds": 12.3,
            "repos_added": 5,
            "repos_updated": 2,
            "repos_skipped": 1,
            "errors": ["timeout: foo/bar"],
        },
    )
    assert update.status_code == 200
    payload = update.json()
    assert payload["repos_added"] == 5
    assert payload["repos_updated"] == 2
    assert payload["errors"] == ["timeout: foo/bar"]
    assert payload["completed_at"] is not None


@pytest.mark.asyncio
async def test_complete_scan_run_404(client: AsyncClient):
    resp = await client.post(
        "/api/ecosystem/scan-runs/missing/complete",
        json={"duration_seconds": 1.0, "repos_added": 0},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_scan_runs_filters_by_strategy(client: AsyncClient):
    await client.post(
        "/api/ecosystem/scan-runs",
        json={"strategy": "incremental"},
    )
    await client.post(
        "/api/ecosystem/scan-runs",
        json={"strategy": "full"},
    )
    await client.post(
        "/api/ecosystem/scan-runs",
        json={"strategy": "trending"},
    )

    inc = await client.get("/api/ecosystem/scan-runs?strategy=incremental")
    assert inc.status_code == 200
    assert inc.json()["total"] == 1

    full = await client.get("/api/ecosystem/scan-runs?strategy=full")
    assert full.json()["total"] == 1

    all_runs = await client.get("/api/ecosystem/scan-runs?limit=10")
    assert all_runs.json()["total"] == 3


@pytest.mark.asyncio
async def test_execute_scan_run_uses_injected_filter(monkeypatch, client: AsyncClient):
    """POST /scan-runs/execute drives the scanner end-to-end with a stubbed gh search."""
    from aiteam.services import ecosystem_scanner

    async def _fake_gh(keyword: str, min_stars: int, topics=None):
        return [
            {
                "repo_full_name": "anthropics/claude-code",
                "name": "claude-code",
                "owner": "anthropics",
                "description": "Claude AI assistant with MCP",
                "stars": 30000,
                "language": "TypeScript",
                "topics": ["claude-code", "mcp"],
                "homepage": None,
                "last_commit_at": datetime.now(tz=UTC),
                "pushed_at": datetime.now(tz=UTC),
                "needs_deep_review": False,
                "relevance_category": "skill-system",
                "relevance_score": 9,
                "one_line_summary": "Claude code",
            }
        ]

    monkeypatch.setattr(ecosystem_scanner, "default_gh_search", _fake_gh)

    # Patch the symbol that the route module imported by name as well.
    import aiteam.api.routes.ecosystem as eco_routes

    monkeypatch.setattr(eco_routes, "default_gh_search", _fake_gh)

    resp = await client.post(
        "/api/ecosystem/scan-runs/execute",
        json={"strategy": "full", "min_stars": 1000, "triggered_by": "manual"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scanned"] >= 1
    assert body["new_profiles"] >= 1
    assert "run_id" in body
    assert isinstance(body["per_query_stats"], dict)


@pytest.mark.asyncio
async def test_execute_scan_run_records_archived(monkeypatch, client: AsyncClient):
    """Repos with old pushed_at must be flagged is_archived=True after execute."""
    old = datetime.now(tz=UTC) - timedelta(days=500)

    async def _fake_gh(keyword: str, min_stars: int, topics=None):
        return [
            {
                "repo_full_name": "stale/agent-lib",
                "name": "agent-lib",
                "owner": "stale",
                "description": "An MCP agent helper",
                "stars": 5000,
                "language": "Python",
                "topics": ["agent", "mcp"],
                "homepage": None,
                "last_commit_at": old,
                "pushed_at": old,
                "needs_deep_review": True,
                "relevance_category": "agent-framework",
                "relevance_score": 6,
                "one_line_summary": "old agent",
            }
        ]

    import aiteam.api.routes.ecosystem as eco_routes

    monkeypatch.setattr(eco_routes, "default_gh_search", _fake_gh)

    resp = await client.post(
        "/api/ecosystem/scan-runs/execute",
        json={"strategy": "full", "min_stars": 1000},
    )
    assert resp.status_code == 200
    assert resp.json()["archived_marked"] == 1

    profile_resp = await client.get(
        "/api/ecosystem/profiles?keyword=agent-lib"
    )
    profiles = profile_resp.json()["profiles"]
    assert any(p["repo_full_name"] == "stale/agent-lib" and p["is_archived"] for p in profiles)
