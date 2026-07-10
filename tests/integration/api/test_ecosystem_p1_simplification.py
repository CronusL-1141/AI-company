"""Integration tests for v1.6.0 P1.A + P1.B simplification.

P1.A: no inactivity filter — stars-only gate, repos are permanent once admitted.
P1.B: layered list returns — summary fields only by default, full via detail=true.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiteam.api.app import create_app
from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

PROJECT_ID = "proj-p1-test-001"


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


# ──────────────────────────────────────────────────────────────
# P1.A — simplified scan profile shape
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_scan_profile_has_popularity_floor(client: AsyncClient) -> None:
    """P1.A: default profile uses popularity_floor, not active_definition."""
    resp = await client.get(
        "/api/ecosystem/scan_profile",
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    profile = resp.json()["scan_profile"]["profile"]
    assert "popularity_floor" in profile
    assert "alert_thresholds" in profile
    # Old fields must be absent from the simplified default
    assert "active_definition" not in profile
    assert "inactive_signals" not in profile
    assert "archive_signals" not in profile


@pytest.mark.asyncio
async def test_default_scan_profile_github_floor_is_1000(client: AsyncClient) -> None:
    """v1.6.0 阈值调整: default github popularity_floor is 1000 (was 5000 in P1.A, lowered per user 2026-05-12)."""
    resp = await client.get(
        "/api/ecosystem/scan_profile",
        headers={"X-Project-Id": PROJECT_ID},
    )
    profile = resp.json()["scan_profile"]["profile"]
    assert profile["popularity_floor"]["github"] == 1000


@pytest.mark.asyncio
async def test_github_archived_synced(client: AsyncClient) -> None:
    """P1.A: repo upserted with is_archived=True shows is_archived in list."""
    await client.post(
        "/api/ecosystem/profiles",
        json={
            "repo_full_name": "owner/archived-repo",
            "name": "archived-repo",
            "owner": "owner",
            "stars": 6000,
            "is_archived": True,
        },
        headers={"X-Project-Id": PROJECT_ID},
    )
    search_resp = await client.get(
        "/api/ecosystem/profiles",
        params={"keyword": "archived-repo"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert search_resp.status_code == 200
    profiles = search_resp.json()["profiles"]
    assert len(profiles) >= 1
    found = next((p for p in profiles if "archived-repo" in p.get("repo_full_name", "")), None)
    assert found is not None
    assert found["is_archived"] is True


@pytest.mark.asyncio
async def test_manual_no_value_marker(client: AsyncClient) -> None:
    """P1.A: POST /repos/{id}/manual_status status='no_value' marks the repo."""
    upsert_resp = await client.post(
        "/api/ecosystem/profiles",
        json={
            "repo_full_name": "owner/low-value-repo",
            "name": "low-value-repo",
            "owner": "owner",
            "stars": 8000,
        },
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert upsert_resp.status_code == 200
    repo_id = upsert_resp.json()["id"]

    mark_resp = await client.post(
        f"/api/ecosystem/repos/{repo_id}/manual_status",
        json={"status": "no_value", "reason": "test reason"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert mark_resp.status_code == 200
    body = mark_resp.json()
    assert body["success"] is True
    assert body["manual_status"] == "no_value"
    assert body["reason"] == "test reason"


@pytest.mark.asyncio
async def test_manual_status_invalid_value_rejected(client: AsyncClient) -> None:
    """P1.A: invalid manual_status values return 400."""
    upsert_resp = await client.post(
        "/api/ecosystem/profiles",
        json={
            "repo_full_name": "owner/some-repo",
            "name": "some-repo",
            "owner": "owner",
            "stars": 5001,
        },
        headers={"X-Project-Id": PROJECT_ID},
    )
    repo_id = upsert_resp.json()["id"]

    bad_resp = await client.post(
        f"/api/ecosystem/repos/{repo_id}/manual_status",
        json={"status": "invalid_value"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert bad_resp.status_code == 400


@pytest.mark.asyncio
async def test_manual_status_clear(client: AsyncClient) -> None:
    """P1.A: clearing manual_status (status=null) succeeds."""
    upsert_resp = await client.post(
        "/api/ecosystem/profiles",
        json={
            "repo_full_name": "owner/clearable-repo",
            "name": "clearable-repo",
            "owner": "owner",
            "stars": 7000,
        },
        headers={"X-Project-Id": PROJECT_ID},
    )
    repo_id = upsert_resp.json()["id"]

    await client.post(
        f"/api/ecosystem/repos/{repo_id}/manual_status",
        json={"status": "no_value", "reason": "test"},
        headers={"X-Project-Id": PROJECT_ID},
    )

    clear_resp = await client.post(
        f"/api/ecosystem/repos/{repo_id}/manual_status",
        json={"status": None, "reason": ""},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["success"] is True
    assert clear_resp.json()["manual_status"] is None


@pytest.mark.asyncio
async def test_manual_status_404_for_unknown_repo(client: AsyncClient) -> None:
    """P1.A: marking unknown repo_id returns 404."""
    resp = await client.post(
        "/api/ecosystem/repos/nonexistent-id-xyz/manual_status",
        json={"status": "no_value", "reason": "test"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────
# P1.B — layered list return
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repos_list_default_limit_20(client: AsyncClient) -> None:
    """P1.B: GET /profiles default limit is 20."""
    for i in range(25):
        await client.post(
            "/api/ecosystem/profiles",
            json={
                "repo_full_name": f"owner/repo-limit-{i:02d}",
                "name": f"repo-limit-{i:02d}",
                "owner": "owner",
                "stars": 5000 + i,
            },
            headers={"X-Project-Id": PROJECT_ID},
        )

    resp = await client.get(
        "/api/ecosystem/profiles",
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["profiles"]) == 20
    assert body["total"] == 25
    assert body["limit"] == 20
    assert body["has_more"] is True


@pytest.mark.asyncio
async def test_repos_list_max_limit_100(client: AsyncClient) -> None:
    """P1.B: requesting limit > 100 is rejected (FastAPI Query le=100)."""
    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"limit": 200},
        headers={"X-Project-Id": PROJECT_ID},
    )
    # FastAPI enforces le=100 → 422 Unprocessable
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_repos_list_returns_excerpt_not_full_summary(client: AsyncClient) -> None:
    """P1.B: list response omits shallow_summary; detail=true includes it."""
    upsert = await client.post(
        "/api/ecosystem/profiles",
        json={
            "repo_full_name": "owner/summary-check-repo",
            "name": "summary-check-repo",
            "owner": "owner",
            "stars": 6000,
        },
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert upsert.status_code == 200

    # Default list: shallow_summary must be absent
    list_resp = await client.get(
        "/api/ecosystem/profiles",
        params={"keyword": "summary-check-repo"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert list_resp.status_code == 200
    profiles = list_resp.json()["profiles"]
    assert len(profiles) >= 1
    item = profiles[0]
    assert "shallow_summary" not in item

    # With detail=true: shallow_summary must be present
    detail_resp = await client.get(
        "/api/ecosystem/profiles",
        params={"keyword": "summary-check-repo", "detail": "true"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert detail_resp.status_code == 200
    detail_profiles = detail_resp.json()["profiles"]
    assert len(detail_profiles) >= 1
    assert "shallow_summary" in detail_profiles[0]


@pytest.mark.asyncio
async def test_repos_list_description_excerpt_max_150(client: AsyncClient) -> None:
    """P1.B: description_excerpt in list mode is at most 150 chars + ellipsis."""
    await client.post(
        "/api/ecosystem/profiles",
        json={
            "repo_full_name": "owner/desc-excerpt-repo",
            "name": "desc-excerpt-repo",
            "owner": "owner",
            "stars": 5500,
            "description": "A" * 300,
        },
        headers={"X-Project-Id": PROJECT_ID},
    )

    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"keyword": "desc-excerpt-repo"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    profiles = resp.json()["profiles"]
    assert len(profiles) >= 1
    excerpt = profiles[0].get("description_excerpt", "")
    assert len(excerpt) <= 153  # 150 chars + "..."


@pytest.mark.asyncio
async def test_description_excerpt_always_within_150_chars(client: AsyncClient) -> None:
    """BUG-P1B-1 regression: list endpoint must enforce 150-char limit on description_excerpt.

    DB may contain stored values >150 from earlier versions; the API layer must truncate.
    Test asserts <=153 (150 + "...") to allow for the ellipsis suffix.
    """
    long_excerpt = "X" * 200  # exceeds 150 chars deliberately
    await client.post(
        "/api/ecosystem/profiles",
        json={
            "repo_full_name": "owner/p1b1-excerpt-test",
            "name": "p1b1-excerpt-test",
            "owner": "owner",
            "stars": 5500,
            "description": long_excerpt,
            "description_excerpt": long_excerpt,  # simulate pre-existing DB value >150
        },
        headers={"X-Project-Id": PROJECT_ID},
    )

    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"keyword": "p1b1-excerpt-test"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    profiles = resp.json()["profiles"]
    assert len(profiles) >= 1
    excerpt = profiles[0].get("description_excerpt") or ""
    assert len(excerpt) <= 153, (
        f"BUG-P1B-1: description_excerpt must be <=153 chars (150+ellipsis), got {len(excerpt)}"
    )


@pytest.mark.asyncio
async def test_data_sources_list_has_pagination_fields(client: AsyncClient) -> None:
    """P1.B: GET /data_sources returns limit/offset/has_more + config_keys (not full config)."""
    await client.post(
        "/api/ecosystem/quick_setup",
        json={
            "sources": ["github"],
            "queries": ["topic:test-p1b"],
            "use_defaults": True,
        },
        headers={"X-Project-Id": PROJECT_ID},
    )

    resp = await client.get(
        "/api/ecosystem/data_sources",
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "total" in body
    assert "limit" in body
    assert "has_more" in body
    if body["data_sources"]:
        ds = body["data_sources"][0]
        # P1.B: config_keys replaces full config for list view
        assert "config_keys" in ds
        assert "config" not in ds


@pytest.mark.asyncio
async def test_index_diffs_history_details_truncated(client: AsyncClient) -> None:
    """P1.B: index_diffs/history returns details_summary truncated to max 5 items."""
    resp = await client.get(
        "/api/ecosystem/index_diffs/history",
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "diffs" in body
    for diff in body["diffs"]:
        assert "details_summary" in diff
        for key, val in diff.get("details_summary", {}).items():
            if isinstance(val, list):
                assert len(val) <= 5, f"details_summary[{key!r}] has {len(val)} items, max 5"


@pytest.mark.asyncio
async def test_agents_list_route_has_pagination_params() -> None:
    """P1.B: agents route signature includes limit/offset/has_more (code-level check).

    Full integration with TeamManager is tested in integration/test_api_integration.py.
    This test validates the route definition is correct by inspecting the router.
    """
    from fastapi.routing import APIRoute

    from aiteam.api.routes.agents import router as agents_router

    # Find the GET /api/teams/{team_id}/agents route
    agent_list_route = None
    for route in agents_router.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods and "agents" in route.path:
            agent_list_route = route
            break

    assert agent_list_route is not None, "Agent list route not found"
    # Verify limit and offset are in the route's dependencies/params
    import inspect
    sig = inspect.signature(agent_list_route.endpoint)
    param_names = list(sig.parameters.keys())
    assert "limit" in param_names, "limit param missing from agents list endpoint"
    assert "offset" in param_names, "offset param missing from agents list endpoint"
