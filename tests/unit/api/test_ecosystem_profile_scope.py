"""Profile writes require resolved project scope before reading or changing rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.mcp import _base
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoProfile

PROFILE = {"repo_full_name": "owner/shared", "owner": "owner", "name": "shared", "stars": 100}
UNSCOPED_HEADERS = [{}, {"X-Project-Id": ""}, {"X-Project-Dir": "/unregistered/project"}]


def _stored(database: Path) -> list[tuple]:
    # A fresh connection observes committed data independently of API responses.
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        return connection.execute(
            "SELECT id, project_id, repo_full_name, stars, description "
            "FROM ecosystem_repo_profiles ORDER BY project_id"
        ).fetchall()


@pytest_asyncio.fixture()
async def eco_scope(tmp_path, monkeypatch):
    database = tmp_path / "profile-scope.sqlite"
    database_url = f"sqlite+aiosqlite:///{database}"
    repo = StorageRepository(db_url=database_url)
    await repo.init_db()
    monkeypatch.setattr(deps, "_repository", repo)
    monkeypatch.setattr(deps, "_project_dir_cache", {})
    parent = await repo.create_project("parent", root_path=str(tmp_path))
    nested = await repo.create_project("nested", root_path=str(tmp_path / "子项目"))
    await deps.refresh_project_dir_cache(repo)
    app = create_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, repo, database, parent, nested
    finally:
        await get_engine(database_url).dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", UNSCOPED_HEADERS, ids=["absent", "empty", "unresolved-dir"])
async def test_unresolved_scope_refuses_profile_creation(eco_scope, headers):
    client, _, database, _, _ = eco_scope

    response = await client.post("/api/ecosystem/profiles", json=PROFILE, headers=headers)

    assert response.status_code == 400, response.text
    assert "X-Project-Id" in response.json()["detail"]
    assert _stored(database) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", UNSCOPED_HEADERS, ids=["absent", "empty", "unresolved-dir"])
async def test_unresolved_scope_does_not_overwrite_global_orphan(eco_scope, headers):
    client, repo, database, _, _ = eco_scope
    orphan = EcosystemRepoProfile(**PROFILE, description="legacy orphan")
    await repo.upsert_ecosystem_profile(orphan)
    before = _stored(database)

    response = await client.post(
        "/api/ecosystem/profiles", json={**PROFILE, "stars": 999, "description": "overwrite"}, headers=headers
    )

    assert response.status_code == 400, response.text
    assert "X-Project-Id" in response.json()["detail"]
    assert _stored(database) == before


@pytest.mark.asyncio
async def test_project_profile_upsert_is_idempotent_and_isolated(eco_scope):
    client, _, database, parent, nested = eco_scope
    parent_headers = {"X-Project-Id": parent.id}
    first = await client.post("/api/ecosystem/profiles", json=PROFILE, headers=parent_headers)
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True
    original_id = _stored(database)[0][0]

    updated = await client.post(
        "/api/ecosystem/profiles", json={**PROFILE, "stars": 200}, headers=parent_headers
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["created"] is False
    assert _stored(database) == [(original_id, parent.id, "owner/shared", 200, None)]

    other = await client.post(
        "/api/ecosystem/profiles", json={**PROFILE, "stars": 300}, headers={"X-Project-Id": nested.id}
    )
    assert other.status_code == 200, other.text
    assert other.json()["created"] is True
    rows = {row[1]: row for row in _stored(database)}
    assert len(rows) == 2
    assert rows[parent.id] == (original_id, parent.id, "owner/shared", 200, None)
    assert rows[nested.id][0] != original_id
    assert rows[nested.id][2:] == ("owner/shared", 300, None)
    for project, stars in ((parent, 200), (nested, 300)):
        listed = await client.get("/api/ecosystem/profiles", headers={"X-Project-Id": project.id})
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == 1
        assert listed.json()["profiles"][0]["stars"] == stars


@pytest.mark.asyncio
async def test_project_dir_resolves_encoded_nested_worktree_scope(eco_scope):
    client, _, database, _, nested = eco_scope
    # MCP sends a percent-encoded cwd, which may be below a registered project.
    headers = {"X-Project-Id": "", "X-Project-Dir": quote(nested.root_path + "/.worktrees/feature")}

    response = await client.post("/api/ecosystem/profiles", json=PROFILE, headers=headers)

    assert response.status_code == 200, response.text
    rows = _stored(database)
    assert len(rows) == 1
    assert rows[0][1:] == (nested.id, "owner/shared", 100, None)


@pytest.mark.parametrize("http_context", [False, True], ids=["stdio-cwd", "http-context-cwd"])
def test_mcp_api_bridge_accepts_registered_cwd_without_project_id(hook_server, tmp_path, monkeypatch, http_context):
    port, database, _ = hook_server
    api_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AITEAM_API_URL", api_url)
    monkeypatch.setattr(_base, "_session_project_id", "")
    root_path = str(tmp_path / "桥接项目")
    cwd = root_path + "/.worktrees/feature"
    monkeypatch.setattr(_base, "PROJECT_DIR", cwd)
    token = _base._http_request_context.set({"project_dir": cwd, "api_url": api_url} if http_context else None)
    try:
        project = _base._api_call("POST", "/api/projects", {"name": "mcp-bridge", "root_path": root_path})
        project_id = project["data"]["id"]
        # The real startup loads this same mapping; the fixture skips production startup.
        monkeypatch.setattr(deps, "_project_dir_cache", {root_path.lower(): project_id})

        response = _base._api_call("POST", "/api/ecosystem/profiles", PROFILE)

        assert response.get("created") is True, response
        rows = _stored(database)
        assert len(rows) == 1
        assert rows[0][1:] == (project_id, "owner/shared", 100, None)
    finally:
        _base._http_request_context.reset(token)
