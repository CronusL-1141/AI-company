"""Unit tests for POST /api/context/resolve endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from aiteam.api.routes.context import _normalize_path

# ── helper to build a minimal test app ─────────────────────────────────────

def _make_app(repo):
    """Return a FastAPI app wired to the given repository."""
    from fastapi import FastAPI

    from aiteam.api.routes.context import router

    app = FastAPI()

    async def _get_repo():
        return repo

    from aiteam.api.deps import get_repository
    app.dependency_overrides[get_repository] = _get_repo
    app.include_router(router)
    return app


# ── _normalize_path unit tests ──────────────────────────────────────────────

def test_normalize_path_forward_slashes():
    result = _normalize_path("C:\\Users\\TUF\\Desktop\\MyProject")
    assert "\\" not in result


def test_normalize_path_strips_trailing_slash():
    result = _normalize_path("/home/user/project/")
    assert not result.endswith("/")


# ── endpoint tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exact_match(db_repository):
    """Exact root_path match returns existing project without creating a new one."""
    project = await db_repository.create_project(
        name="MyProject",
        root_path="C:/Users/TUF/Desktop/MyProject",
    )
    app = _make_app(db_repository)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/context/resolve",
            json={"cwd": "C:/Users/TUF/Desktop/MyProject"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == project.id
    assert data["created"] is False


@pytest.mark.asyncio
async def test_prefix_match(db_repository):
    """cwd inside project root resolves to that project."""
    project = await db_repository.create_project(
        name="MyProject",
        root_path="C:/Users/TUF/Desktop/MyProject",
    )
    app = _make_app(db_repository)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/context/resolve",
            json={"cwd": "C:/Users/TUF/Desktop/MyProject/subdir"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == project.id
    assert data["created"] is False


@pytest.mark.asyncio
async def test_auto_create(db_repository):
    """Unknown cwd with EXPLICIT auto_create=True creates a new project."""
    app = _make_app(db_repository)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/context/resolve",
            json={"cwd": "C:/Users/TUF/Desktop/靖安笔试", "auto_create": True},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] != ""
    assert data["project_name"] == "靖安笔试"
    assert data["created"] is True

    # Calling again must be idempotent — no duplicate created
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp2 = await client.post(
            "/api/context/resolve",
            json={"cwd": "C:/Users/TUF/Desktop/靖安笔试", "auto_create": True},
        )
    data2 = resp2.json()
    assert data2["project_id"] == data["project_id"]
    assert data2["created"] is False


@pytest.mark.asyncio
async def test_default_never_auto_creates(db_repository):
    """归属铁律（2026-07-08）：缺省请求匹配不到项目时留空，绝不自动立项——
    hook 每次工具调用都打这个端点，默认 True 曾把产出物目录注册成项目。"""
    app = _make_app(db_repository)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/context/resolve",
            json={"cwd": "C:/Users/TUF/Desktop/某产出物目录/demo"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == ""
    assert data["created"] is False


@pytest.mark.asyncio
async def test_no_auto_create_returns_empty(db_repository):
    """Unknown cwd with auto_create=False returns empty project_id."""
    app = _make_app(db_repository)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/context/resolve",
            json={"cwd": "C:/Users/TUF/Desktop/NonExistent", "auto_create": False},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == ""
    assert data["created"] is False


@pytest.mark.asyncio
async def test_longest_prefix_wins(db_repository):
    """When multiple projects are prefixes, the longest match wins."""
    _ = await db_repository.create_project(        name="Parent",
        root_path="C:/Users/TUF/Desktop",
    )
    child = await db_repository.create_project(
        name="Child",
        root_path="C:/Users/TUF/Desktop/MyProject",
    )
    app = _make_app(db_repository)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/context/resolve",
            json={"cwd": "C:/Users/TUF/Desktop/MyProject/src"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == child.id
