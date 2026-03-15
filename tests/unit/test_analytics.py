"""AI Team OS — Analytics API 测试.

测试活动分析统计端点：工具使用分布、Agent产能、时间线、团队概览。
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

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)
    event_bus = EventBus(repo=repo)

    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager

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


def _setup_team_with_activities(client) -> tuple[str, str]:
    """创建团队、Agent和活动数据，返回 (team_id, agent_id)."""
    # 创建团队
    resp = client.post("/api/teams", json={"name": "analytics-team"})
    team_id = resp.json()["data"]["id"]
    team_name = resp.json()["data"]["name"]

    # 添加Agent
    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "dev-1", "role": "开发"},
    )
    agent_id = resp.json()["data"]["id"]

    # 添加活动记录
    import asyncio

    repo = deps._repository

    async def add_activities():
        await repo.create_activity(agent_id, "session-1", "Bash", "ls -la", "file list")
        await repo.create_activity(agent_id, "session-1", "Read", "main.py", "content...")
        await repo.create_activity(agent_id, "session-1", "Bash", "pytest", "passed")
        await repo.create_activity(agent_id, "session-1", "Edit", "main.py", "updated")
        await repo.create_activity(agent_id, "session-1", "Read", "config.py", "content...")

    asyncio.get_event_loop().run_until_complete(add_activities())
    return team_id, agent_id


# ============================================================
# 工具使用分布
# ============================================================


def test_tool_usage_empty(app_client):
    """无活动数据时返回空列表."""
    resp = app_client.get("/api/analytics/tool-usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"] == []


def test_tool_usage_with_data(app_client):
    """有活动数据时返回按计数排序的工具分布."""
    team_id, _ = _setup_team_with_activities(app_client)

    resp = app_client.get("/api/analytics/tool-usage")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 3  # Bash, Read, Edit
    # Bash和Read各2次（排序不确定），Edit 1次
    counts = {item["tool_name"]: item["count"] for item in data}
    assert counts["Bash"] == 2
    assert counts["Read"] == 2
    assert counts["Edit"] == 1


def test_tool_usage_filter_by_team(app_client):
    """按团队筛选工具使用."""
    team_id, _ = _setup_team_with_activities(app_client)

    resp = app_client.get(f"/api/analytics/tool-usage?team_id={team_id}")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 3


# ============================================================
# Agent产能
# ============================================================


def test_agent_productivity_empty(app_client):
    """无数据时返回空."""
    resp = app_client.get("/api/analytics/agent-productivity")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_agent_productivity_with_data(app_client):
    """有数据时返回每Agent的产能指标."""
    team_id, agent_id = _setup_team_with_activities(app_client)

    resp = app_client.get("/api/analytics/agent-productivity")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["agent_name"] == "dev-1"
    assert data[0]["activity_count"] == 5
    assert data[0]["tools_used"] == 3  # Bash, Read, Edit
    assert data[0]["last_active"] is not None


# ============================================================
# 活动时间线
# ============================================================


def test_timeline_empty(app_client):
    """无数据时返回空."""
    resp = app_client.get("/api/analytics/timeline")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_timeline_with_data(app_client):
    """有数据时按小时聚合."""
    _setup_team_with_activities(app_client)

    resp = app_client.get("/api/analytics/timeline?hours=1")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 所有活动在同一小时内
    assert len(data) >= 1
    total = sum(item["count"] for item in data)
    assert total == 5


def test_timeline_hours_validation(app_client):
    """hours参数范围限制 (1-168)."""
    resp = app_client.get("/api/analytics/timeline?hours=0")
    assert resp.status_code == 422  # validation error

    resp = app_client.get("/api/analytics/timeline?hours=200")
    assert resp.status_code == 422


# ============================================================
# 团队概览
# ============================================================


def test_team_overview_requires_team_id(app_client):
    """team_id是必填参数."""
    resp = app_client.get("/api/analytics/team-overview")
    assert resp.status_code == 422


def test_team_overview_with_data(app_client):
    """返回完整的团队概览统计."""
    team_id, _ = _setup_team_with_activities(app_client)

    resp = app_client.get(f"/api/analytics/team-overview?team_id={team_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_activities"] == 5
    assert data["total_agents"] == 1
    assert isinstance(data["tool_distribution"], list)
    assert isinstance(data["agent_productivity"], list)


# ============================================================
# 效率指标
# ============================================================


def test_efficiency_empty(app_client):
    """无数据时返回零值效率指标."""
    resp = app_client.get("/api/analytics/efficiency")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["task_completion"]["total_tasks"] == 0
    assert data["task_completion"]["completion_rate"] == 0
    assert data["avg_tools_per_task"] is None
    assert data["agent_utilization"] == []
    assert data["top_agents"] == []


def test_efficiency_with_tasks(app_client):
    """有任务和活动数据时返回正确的效率指标."""
    team_id, agent_id = _setup_team_with_activities(app_client)

    # 创建几个任务并完成部分
    import asyncio
    from datetime import datetime, timedelta

    repo = deps._repository

    async def add_tasks():
        t1 = await repo.create_task(team_id, "任务1", assigned_to=agent_id)
        t2 = await repo.create_task(team_id, "任务2", assigned_to=agent_id)
        t3 = await repo.create_task(team_id, "任务3")
        # 完成t1和t2
        await repo.update_task(
            t1.id, status="completed", completed_at=datetime.now()
        )
        await repo.update_task(
            t2.id, status="completed", completed_at=datetime.now()
        )

    asyncio.get_event_loop().run_until_complete(add_tasks())

    resp = app_client.get(f"/api/analytics/efficiency?team_id={team_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]

    # 任务完成率：2/3
    tc = data["task_completion"]
    assert tc["total_tasks"] == 3
    assert tc["completed_tasks"] == 2
    assert 0.66 <= tc["completion_rate"] <= 0.67

    # 平均工具调用/任务：5活动 / 2完成任务 = 2.5
    assert data["avg_tools_per_task"] == 2.5

    # Agent利用率
    assert len(data["agent_utilization"]) == 1
    assert data["agent_utilization"][0]["agent_name"] == "dev-1"
    assert data["agent_utilization"][0]["activity_count"] == 5

    # 最高效排行
    assert len(data["top_agents"]) == 1


def test_efficiency_filter_by_team(app_client):
    """按团队筛选效率指标."""
    team_id, _ = _setup_team_with_activities(app_client)

    resp = app_client.get(f"/api/analytics/efficiency?team_id={team_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["agent_utilization"][0]["agent_name"] == "dev-1"

    # 查询不存在的团队
    resp = app_client.get("/api/analytics/efficiency?team_id=nonexistent")
    assert resp.status_code == 200
    assert resp.json()["data"]["agent_utilization"] == []
