"""AI Team OS — 任务Memo追踪单元测试."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def app_client():
    """创建测试客户端，使用内存SQLite."""
    import asyncio

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)

    event_bus = EventBus(repo=repo)
    hook_translator = HookTranslator(repo=repo, event_bus=event_bus)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._hook_translator = hook_translator

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan

    client = TestClient(app)
    yield client

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


def _create_team_and_task(client: TestClient) -> tuple[str, str]:
    """辅助：创建团队和任务，返回 (team_id, task_id)."""
    team_resp = client.post("/api/teams", json={"name": "memo-test-team"})
    team_id = team_resp.json()["data"]["id"]

    task_resp = client.post(f"/api/teams/{team_id}/tasks/run", json={
        "title": "测试任务",
        "description": "用于memo测试",
    })
    task_id = task_resp.json()["data"]["id"]
    return team_id, task_id


def test_empty_memo(app_client: TestClient):
    """空memo返回空列表."""
    _, task_id = _create_team_and_task(app_client)
    resp = app_client.get(f"/api/tasks/{task_id}/memo")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"] == []


def test_add_memo(app_client: TestClient):
    """POST追加memo."""
    _, task_id = _create_team_and_task(app_client)
    resp = app_client.post(f"/api/tasks/{task_id}/memo", json={
        "content": "开始实施任务",
        "type": "progress",
        "author": "engineer-1",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["content"] == "开始实施任务"
    assert data["data"]["type"] == "progress"
    assert data["data"]["author"] == "engineer-1"
    assert "timestamp" in data["data"]


def test_read_memo(app_client: TestClient):
    """GET读取memo列表."""
    _, task_id = _create_team_and_task(app_client)

    # 追加两条
    app_client.post(f"/api/tasks/{task_id}/memo", json={
        "content": "第一条",
    })
    app_client.post(f"/api/tasks/{task_id}/memo", json={
        "content": "第二条",
    })

    resp = app_client.get(f"/api/tasks/{task_id}/memo")
    assert resp.status_code == 200
    memos = resp.json()["data"]
    assert len(memos) == 2
    assert memos[0]["content"] == "第一条"
    assert memos[1]["content"] == "第二条"


def test_memo_types(app_client: TestClient):
    """不同type正确存储."""
    _, task_id = _create_team_and_task(app_client)

    for memo_type in ("progress", "decision", "issue", "summary"):
        app_client.post(f"/api/tasks/{task_id}/memo", json={
            "content": f"类型={memo_type}",
            "type": memo_type,
        })

    resp = app_client.get(f"/api/tasks/{task_id}/memo")
    memos = resp.json()["data"]
    assert len(memos) == 4
    types = [m["type"] for m in memos]
    assert types == ["progress", "decision", "issue", "summary"]


def test_memo_not_found(app_client: TestClient):
    """不存在的任务返回404."""
    resp = app_client.get("/api/tasks/nonexistent-id/memo")
    assert resp.status_code == 404

    resp = app_client.post("/api/tasks/nonexistent-id/memo", json={
        "content": "test",
    })
    assert resp.status_code == 404
