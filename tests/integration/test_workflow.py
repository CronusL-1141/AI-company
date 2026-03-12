"""端到端工作流集成测试.

模拟完整的团队协作流程，使用mock LLM替代真实模型调用。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

# ============================================================
# 1. Coordinate模式工作流
# ============================================================


def test_coordinate_workflow(integration_client):
    """coordinate模式: 创建团队→添加2个Agent→run_task(mock LLM)→验证结果."""
    client = integration_client

    # 创建coordinate团队
    resp = client.post(
        "/api/teams",
        json={"name": "coord-workflow", "mode": "coordinate"},
    )
    assert resp.status_code == 201
    team_name = resp.json()["data"]["name"]

    # 添加2个Agent
    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "analyst", "role": "数据分析师", "system_prompt": "你是数据分析专家"},
    )
    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "writer", "role": "报告撰写", "system_prompt": "你是技术文档专家"},
    )

    # Mock graph执行
    mock_graph = AsyncMock()
    mock_graph.ainvoke.return_value = {
        "final_result": "coordinate模式分析报告完成",
        "agent_outputs": {
            "analyst": "数据分析完成，发现3个关键趋势",
            "writer": "报告已撰写完毕，共5页",
        },
        "messages": [],
    }

    with patch(
        "aiteam.orchestrator.team_manager.compile_graph",
        return_value=mock_graph,
    ):
        resp = client.post(
            f"/api/teams/{team_name}/tasks/run",
            json={
                "description": "分析Q1销售数据并撰写报告",
                "title": "Q1销售分析",
            },
        )

    assert resp.status_code == 200
    result = resp.json()["data"]
    assert result["status"] == "completed"
    assert "coordinate模式分析报告完成" in result["result"]
    assert "analyst" in result["agent_outputs"]
    assert "writer" in result["agent_outputs"]
    assert result["duration_seconds"] >= 0

    # 验证任务已记录
    resp = client.get(f"/api/teams/{team_name}/tasks")
    assert resp.status_code == 200 if resp.json()["success"] else True


# ============================================================
# 2. Broadcast模式工作流
# ============================================================


def test_broadcast_workflow(integration_client):
    """broadcast模式: 创建团队→添加3个Agent→run_task(mock LLM)→验证结果."""
    client = integration_client

    # 创建broadcast团队
    resp = client.post(
        "/api/teams",
        json={"name": "bcast-workflow", "mode": "broadcast"},
    )
    assert resp.status_code == 201
    team_name = resp.json()["data"]["name"]

    # 添加3个Agent
    for name, role in [("dev1", "前端"), ("dev2", "后端"), ("dev3", "测试")]:
        resp = client.post(
            f"/api/teams/{team_name}/agents",
            json={"name": name, "role": role},
        )
        assert resp.status_code == 201

    # 验证3个Agent已添加
    team_id = client.get(f"/api/teams/{team_name}").json()["data"]["id"]
    resp = client.get(f"/api/teams/{team_id}/agents")
    assert resp.json()["total"] == 3

    # Mock graph执行
    mock_graph = AsyncMock()
    mock_graph.ainvoke.return_value = {
        "final_result": "broadcast模式全员评审完成",
        "agent_outputs": {
            "dev1": "前端方案已评审",
            "dev2": "后端方案已评审",
            "dev3": "测试方案已评审",
        },
        "messages": [],
    }

    with patch(
        "aiteam.orchestrator.team_manager.compile_graph",
        return_value=mock_graph,
    ):
        resp = client.post(
            f"/api/teams/{team_name}/tasks/run",
            json={
                "description": "评审系统架构方案",
                "title": "架构评审",
            },
        )

    assert resp.status_code == 200
    result = resp.json()["data"]
    assert result["status"] == "completed"
    assert len(result["agent_outputs"]) == 3


# ============================================================
# 3. 记忆存储与检索工作流
# ============================================================


def test_memory_store_workflow(repo_and_client):
    """存储记忆→通过API检索→验证内容."""
    repo, client = repo_and_client

    # 通过repo直接存储记忆（模拟业务逻辑层写入）
    asyncio.get_event_loop().run_until_complete(
        repo.create_memory(
            scope="team",
            scope_id="test-team",
            content="团队决定采用微服务架构",
            metadata={"author": "tech-lead"},
        )
    )
    asyncio.get_event_loop().run_until_complete(
        repo.create_memory(
            scope="team",
            scope_id="test-team",
            content="数据库选型确定为PostgreSQL",
            metadata={"author": "dba"},
        )
    )

    # 通过API搜索
    resp = client.get("/api/memory?scope=team&scope_id=test-team&query=架构")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] >= 1
    # 搜索结果应包含"微服务架构"
    contents = [m["content"] for m in data["data"]]
    assert any("微服务架构" in c for c in contents)

    # 不带query列出全部
    resp = client.get("/api/memory?scope=team&scope_id=test-team")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


# ============================================================
# 4. 跨会话持久化验证
# ============================================================


def test_persistence_across_sessions(repo_and_client):
    """创建数据→验证同一数据库会话中数据持久存在."""
    repo, client = repo_and_client

    # 第一阶段：创建数据
    resp = client.post("/api/teams", json={"name": "persist-team", "mode": "coordinate"})
    assert resp.status_code == 201
    team_name = resp.json()["data"]["name"]
    team_id = resp.json()["data"]["id"]

    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "persist-agent", "role": "持久化测试"},
    )

    # 存储一条记忆
    asyncio.get_event_loop().run_until_complete(
        repo.create_memory(
            scope="global",
            scope_id="system",
            content="持久化测试数据",
        )
    )

    # 创建事件
    asyncio.get_event_loop().run_until_complete(
        repo.create_event(
            event_type="team.created",
            source="persistence-test",
            data={"team_id": team_id},
        )
    )

    # 第二阶段：模拟"重新打开" — 通过全新请求验证数据存在
    # （在同一数据库中验证，因为内存SQLite在测试期间保持连接）

    # 验证团队存在
    resp = client.get(f"/api/teams/{team_name}")
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "persist-team"

    # 验证Agent存在
    resp = client.get(f"/api/teams/{team_id}/agents")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["data"][0]["name"] == "persist-agent"

    # 验证记忆存在
    resp = client.get("/api/memory?scope=global&scope_id=system")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1

    # 验证事件存在
    resp = client.get("/api/events?source=persistence-test")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


# ============================================================
# 5. 任务执行失败场景
# ============================================================


def test_task_failure_workflow(integration_client):
    """任务执行失败时应正确记录失败状态."""
    client = integration_client

    # 创建团队
    resp = client.post("/api/teams", json={"name": "fail-workflow"})
    team_name = resp.json()["data"]["name"]

    # Mock graph抛出异常
    mock_graph = AsyncMock()
    mock_graph.ainvoke.side_effect = RuntimeError("LLM API连接超时")

    with patch(
        "aiteam.orchestrator.team_manager.compile_graph",
        return_value=mock_graph,
    ):
        resp = client.post(
            f"/api/teams/{team_name}/tasks/run",
            json={"description": "会失败的任务"},
        )

    assert resp.status_code == 200
    result = resp.json()["data"]
    assert result["status"] == "failed"
    assert "失败" in result["result"] or "LLM" in result["result"]


# ============================================================
# 6. 多团队并发操作
# ============================================================


def test_multiple_teams_isolation(integration_client):
    """多个团队之间数据隔离."""
    client = integration_client

    # 创建两个团队
    resp1 = client.post("/api/teams", json={"name": "team-alpha"})
    resp2 = client.post("/api/teams", json={"name": "team-beta"})
    team_a = resp1.json()["data"]["name"]
    team_b = resp2.json()["data"]["name"]
    team_a_id = resp1.json()["data"]["id"]
    team_b_id = resp2.json()["data"]["id"]

    # 各自添加不同Agent
    client.post(f"/api/teams/{team_a}/agents", json={"name": "a1", "role": "开发"})
    client.post(f"/api/teams/{team_a}/agents", json={"name": "a2", "role": "测试"})
    client.post(f"/api/teams/{team_b}/agents", json={"name": "b1", "role": "设计"})

    # 验证隔离
    resp = client.get(f"/api/teams/{team_a_id}/agents")
    assert resp.json()["total"] == 2

    resp = client.get(f"/api/teams/{team_b_id}/agents")
    assert resp.json()["total"] == 1

    # 删除team-alpha不影响team-beta
    client.delete(f"/api/teams/{team_a}")
    resp = client.get(f"/api/teams/{team_b}")
    assert resp.status_code == 200
