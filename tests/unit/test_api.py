"""AI Team OS — API单元测试.

使用FastAPI TestClient和内存SQLite测试REST API。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def app_client():
    """创建测试客户端，使用内存SQLite."""
    import asyncio

    # 初始化依赖
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)

    # 注入到deps模块（含EventBus）
    event_bus = EventBus(repo=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager

    app = create_app()

    # 覆盖lifespan：测试中不需要自动init/cleanup
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan

    client = TestClient(app)
    yield client

    # 清理
    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None


# ============================================================
# 团队 CRUD
# ============================================================


def test_create_team(app_client):
    """测试创建团队."""
    resp = app_client.post("/api/teams", json={"name": "test-team", "mode": "coordinate"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["name"] == "test-team"
    assert data["data"]["mode"] == "coordinate"
    assert "id" in data["data"]


def test_list_teams(app_client):
    """测试列出团队."""
    # 先创建两个团队
    app_client.post("/api/teams", json={"name": "team-a"})
    app_client.post("/api/teams", json={"name": "team-b"})

    resp = app_client.get("/api/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] >= 2


def test_get_team(app_client):
    """测试获取团队详情."""
    create_resp = app_client.post("/api/teams", json={"name": "get-team"})
    team_name = create_resp.json()["data"]["name"]

    resp = app_client.get(f"/api/teams/{team_name}")
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "get-team"


def test_update_team(app_client):
    """测试更新团队模式."""
    create_resp = app_client.post("/api/teams", json={"name": "update-team"})
    team_name = create_resp.json()["data"]["name"]

    resp = app_client.put(f"/api/teams/{team_name}", json={"mode": "broadcast"})
    assert resp.status_code == 200
    assert resp.json()["data"]["mode"] == "broadcast"


def test_delete_team(app_client):
    """测试删除团队."""
    create_resp = app_client.post("/api/teams", json={"name": "del-team"})
    team_name = create_resp.json()["data"]["name"]

    resp = app_client.delete(f"/api/teams/{team_name}")
    assert resp.status_code == 200
    assert resp.json()["data"] is True

    # 再查应该404
    resp = app_client.get(f"/api/teams/{team_name}")
    assert resp.status_code == 404


def test_get_team_status(app_client):
    """测试获取团队状态."""
    create_resp = app_client.post("/api/teams", json={"name": "status-team"})
    team_name = create_resp.json()["data"]["name"]

    resp = app_client.get(f"/api/teams/{team_name}/status")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["team"]["name"] == "status-team"
    assert data["completed_tasks"] == 0


# ============================================================
# Agent 管理
# ============================================================


def test_create_agent(app_client):
    """测试添加Agent."""
    create_resp = app_client.post("/api/teams", json={"name": "agent-team"})
    team_name = create_resp.json()["data"]["name"]

    resp = app_client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "dev-1", "role": "后端开发"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["data"]["name"] == "dev-1"
    assert data["data"]["role"] == "后端开发"


def test_list_agents(app_client):
    """测试列出Agent."""
    create_resp = app_client.post("/api/teams", json={"name": "agents-list-team"})
    team_id = create_resp.json()["data"]["id"]

    app_client.post(
        f"/api/teams/{create_resp.json()['data']['name']}/agents",
        json={"name": "a1", "role": "前端"},
    )
    app_client.post(
        f"/api/teams/{create_resp.json()['data']['name']}/agents",
        json={"name": "a2", "role": "后端"},
    )

    resp = app_client.get(f"/api/teams/{team_id}/agents")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


def test_delete_agent(app_client):
    """测试删除Agent."""
    create_resp = app_client.post("/api/teams", json={"name": "del-agent-team"})
    team_name = create_resp.json()["data"]["name"]

    agent_resp = app_client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "to-delete", "role": "测试"},
    )
    agent_id = agent_resp.json()["data"]["id"]

    resp = app_client.delete(f"/api/agents/{agent_id}")
    assert resp.status_code == 200
    assert resp.json()["data"] is True


# ============================================================
# 任务管理
# ============================================================


def test_list_tasks(app_client):
    """测试列出任务（空列表）."""
    create_resp = app_client.post("/api/teams", json={"name": "task-team"})
    team_id = create_resp.json()["data"]["id"]

    resp = app_client.get(f"/api/teams/{team_id}/tasks")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_run_task_mock(app_client):
    """测试运行任务（mock LLM）."""
    create_resp = app_client.post("/api/teams", json={"name": "run-team"})
    team_name = create_resp.json()["data"]["name"]

    # Mock compile_graph 返回一个模拟的graph
    mock_graph = AsyncMock()
    mock_graph.ainvoke.return_value = {
        "final_result": "模拟结果",
        "agent_outputs": {"agent-1": "输出"},
    }

    with patch("aiteam.orchestrator.team_manager.compile_graph", return_value=mock_graph):
        resp = app_client.post(
            f"/api/teams/{team_name}/tasks/run",
            json={"description": "测试任务", "title": "测试"},
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "completed"
    assert data["result"] == "模拟结果"


def test_get_task_status(app_client):
    """测试查询任务状态."""
    create_resp = app_client.post("/api/teams", json={"name": "task-status-team"})
    team_name = create_resp.json()["data"]["name"]

    mock_graph = AsyncMock()
    mock_graph.ainvoke.return_value = {
        "final_result": "完成",
        "agent_outputs": {},
    }

    with patch("aiteam.orchestrator.team_manager.compile_graph", return_value=mock_graph):
        run_resp = app_client.post(
            f"/api/teams/{team_name}/tasks/run",
            json={"description": "状态查询测试"},
        )

    task_id = run_resp.json()["data"]["task_id"]
    resp = app_client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == task_id


# ============================================================
# 事件
# ============================================================


def test_list_events(app_client):
    """测试列出事件（空列表）."""
    resp = app_client.get("/api/events")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_list_events_with_filter(app_client):
    """测试事件过滤参数."""
    resp = app_client.get("/api/events?type=team.created&source=api&limit=10")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ============================================================
# 记忆
# ============================================================


def test_search_memories(app_client):
    """测试搜索记忆."""
    resp = app_client.get("/api/memory?scope=global&scope_id=system")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ============================================================
# 错误处理
# ============================================================


def test_error_team_not_found(app_client):
    """测试团队不存在返回404."""
    resp = app_client.get("/api/teams/nonexistent-id-12345")
    assert resp.status_code == 404
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "not_found"


def test_error_task_not_found(app_client):
    """测试任务不存在返回404."""
    resp = app_client.get("/api/tasks/nonexistent-task-id")
    assert resp.status_code == 404


# ============================================================
# CORS和OpenAPI
# ============================================================


def test_cors_headers(app_client):
    """测试CORS响应头."""
    resp = app_client.options(
        "/api/teams",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_openapi_docs_accessible(app_client):
    """测试OpenAPI文档可访问."""
    resp = app_client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["info"]["title"] == "AI Team OS"
    assert "/api/teams" in data["paths"]
