"""Integration tests for v1.6.0 P0.2 ecosystem API endpoints."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiteam.api.app import create_app
from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

PROJECT_ID = "proj-v4-test-001"


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
        yield ac


# ---------------------------------------------------------------
# POST /api/ecosystem/data_sources
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_data_source(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/ecosystem/data_sources",
        json={"kind": "github", "name": "GitHub default", "config": {"queries": ["topic:mcp"]}},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    ds = body["data_source"]
    assert ds["kind"] == "github"
    assert ds["name"] == "GitHub default"
    assert ds["enabled"] is True
    assert ds["version"] == 1
    assert ds["id"]


@pytest.mark.asyncio
async def test_create_data_source_invalid_kind(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/ecosystem/data_sources",
        json={"kind": "nonexistent_source", "name": "bad"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------
# GET /api/ecosystem/data_sources
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_data_sources_empty(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/ecosystem/data_sources",
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["data_sources"] == []


@pytest.mark.asyncio
async def test_list_data_sources_after_create(client: AsyncClient) -> None:
    await client.post(
        "/api/ecosystem/data_sources",
        json={"kind": "github", "name": "gh"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    await client.post(
        "/api/ecosystem/data_sources",
        json={"kind": "npm", "name": "npm"},
        headers={"X-Project-Id": PROJECT_ID},
    )

    resp = await client.get(
        "/api/ecosystem/data_sources",
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2


# ---------------------------------------------------------------
# PUT /api/ecosystem/data_sources/{id}
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_data_source(client: AsyncClient) -> None:
    create_resp = await client.post(
        "/api/ecosystem/data_sources",
        json={"kind": "github", "name": "original"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    ds_id = create_resp.json()["data_source"]["id"]

    update_resp = await client.put(
        f"/api/ecosystem/data_sources/{ds_id}",
        json={"name": "updated name", "config": {"queries": ["topic:claude"]}},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert update_resp.status_code == 200
    ds = update_resp.json()["data_source"]
    assert ds["name"] == "updated name"
    assert ds["version"] == 2


@pytest.mark.asyncio
async def test_update_data_source_not_found(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/ecosystem/data_sources/does-not-exist",
        json={"name": "x"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------
# GET /api/ecosystem/scan_profile
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scan_profile_returns_default_when_none(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/ecosystem/scan_profile",
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["is_default"] is True
    profile = body["scan_profile"]
    assert profile["id"] is None
    # v1.6.0-P1.A: simplified profile — stars gate only, no inactivity signals
    assert "popularity_floor" in profile["profile"]
    assert "alert_thresholds" in profile["profile"]
    assert "active_definition" not in profile["profile"]
    assert "inactive_signals" not in profile["profile"]


@pytest.mark.asyncio
async def test_get_scan_profile_returns_configured(client: AsyncClient) -> None:
    await client.put(
        "/api/ecosystem/scan_profile",
        json={"profile": {"active_definition": {"active_top_n_per_source": 100}}},
        headers={"X-Project-Id": PROJECT_ID},
    )

    resp = await client.get(
        "/api/ecosystem/scan_profile",
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_default"] is False
    assert body["scan_profile"]["profile"]["active_definition"]["active_top_n_per_source"] == 100


# ---------------------------------------------------------------
# PUT /api/ecosystem/scan_profile
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_scan_profile_creates_version(client: AsyncClient) -> None:
    resp1 = await client.put(
        "/api/ecosystem/scan_profile",
        json={"profile": {"active_definition": {"active_top_n_per_source": 200}}},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp1.status_code == 200
    assert resp1.json()["scan_profile"]["version"] == 1

    resp2 = await client.put(
        "/api/ecosystem/scan_profile",
        json={"profile": {"active_definition": {"active_top_n_per_source": 150}}},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp2.status_code == 200
    assert resp2.json()["scan_profile"]["version"] == 2


# ---------------------------------------------------------------
# POST /api/ecosystem/quick_setup
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_quick_setup_creates_source_and_profile(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/ecosystem/quick_setup",
        json={
            "sources": ["github"],
            "queries": ["topic:claude-code", "topic:mcp-server"],
            "use_defaults": True,
        },
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["data_source_ids"]) == 1
    assert body["scan_profile_id"]
    assert body["scan_profile_version"] == 1


@pytest.mark.asyncio
async def test_quick_setup_multiple_sources(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/ecosystem/quick_setup",
        json={"sources": ["github", "npm", "pypi"], "use_defaults": True},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data_source_ids"]) == 3


@pytest.mark.asyncio
async def test_quick_setup_invalid_source_kind(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/ecosystem/quick_setup",
        json={"sources": ["invalid_kind"]},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------
# POST /api/ecosystem/index_update
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_update_runs_without_data_source_or_scan_profile(
    client: AsyncClient,
) -> None:
    """Q5: config comes from settings — the two dead tables are no longer gates.

    Both tables were zeroed by the v1.6.1 single-source ruling, so the old hard
    check wedged this endpoint (and every downstream diff reader) at
    ``missing_setup`` forever.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    gh_auth_ok = MagicMock(returncode=0)
    with (
        patch("subprocess.run", return_value=gh_auth_ok),
        patch(
            "aiteam.api.routes.ecosystem.default_gh_search",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "aiteam.services.ecosystem_scanner.default_gh_search",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.post(
            "/api/ecosystem/index_update",
            json={"dry_run": True},
            headers={"X-Project-Id": PROJECT_ID},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body.get("missing_setup", []) == []
    # settings-driven config is echoed back
    assert body["min_stars"] > 0
    assert body["query_count"] > 0


@pytest.mark.asyncio
async def test_index_update_after_setup_returns_success(client: AsyncClient) -> None:
    """After quick_setup, index_update with mocked gh auth + empty scan returns success."""
    from unittest.mock import AsyncMock, MagicMock, patch

    # Setup first
    await client.post(
        "/api/ecosystem/quick_setup",
        json={"sources": ["github"], "use_defaults": True},
        headers={"X-Project-Id": PROJECT_ID},
    )

    gh_auth_ok = MagicMock(returncode=0)
    with (
        patch("subprocess.run", return_value=gh_auth_ok),
        patch(
            "aiteam.api.routes.ecosystem.default_gh_search",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "aiteam.services.ecosystem_scanner.default_gh_search",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.post(
            "/api/ecosystem/index_update",
            json={"dry_run": True},
            headers={"X-Project-Id": PROJECT_ID},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["dry_run"] is True
    assert body.get("missing_setup", []) == []


@pytest.mark.asyncio
async def test_index_update_uses_settings_focus_topics_as_queries(
    client: AsyncClient,
) -> None:
    """focus_topics in settings drive the query set (empty → DEFAULT_QUERIES)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    await client.put(
        f"/api/ecosystem/projects/{PROJECT_ID}/settings",
        json={"focus_topics": ["mcp", "claude-code"]},
        headers={"X-Project-Id": PROJECT_ID},
    )

    gh_auth_ok = MagicMock(returncode=0)
    gh_search = AsyncMock(return_value=[])
    with (
        patch("subprocess.run", return_value=gh_auth_ok),
        patch("aiteam.api.routes.ecosystem.default_gh_search", new=gh_search),
        patch("aiteam.services.ecosystem_scanner.default_gh_search", new=gh_search),
    ):
        resp = await client.post(
            "/api/ecosystem/index_update",
            json={"dry_run": True},
            headers={"X-Project-Id": PROJECT_ID},
        )

    body = resp.json()
    assert body["success"] is True
    assert body["query_count"] == 2
    searched_topics = sorted(call.args[2][0] for call in gh_search.call_args_list)
    assert searched_topics == ["claude-code", "mcp"]
