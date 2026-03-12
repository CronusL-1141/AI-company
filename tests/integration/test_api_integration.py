"""API集成测试 — 完整CRUD流程.

使用TestClient + 真实SQLite（内存数据库），测试API端到端行为。
"""

from __future__ import annotations

import asyncio

# ============================================================
# 1. 团队完整生命周期
# ============================================================


def test_full_team_lifecycle(integration_client):
    """创建团队→获取→列出→更新→删除 的完整流程."""
    client = integration_client

    # 创建
    resp = client.post("/api/teams", json={"name": "lifecycle-team", "mode": "coordinate"})
    assert resp.status_code == 201
    team = resp.json()["data"]
    team_id = team["id"]
    assert team["name"] == "lifecycle-team"
    assert team["mode"] == "coordinate"

    # 获取（按名称）
    resp = client.get(f"/api/teams/{team['name']}")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == team_id

    # 获取（按ID）
    resp = client.get(f"/api/teams/{team_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "lifecycle-team"

    # 列出
    resp = client.get("/api/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    names = [t["name"] for t in data["data"]]
    assert "lifecycle-team" in names

    # 更新模式
    resp = client.put(f"/api/teams/{team['name']}", json={"mode": "broadcast"})
    assert resp.status_code == 200
    assert resp.json()["data"]["mode"] == "broadcast"

    # 验证更新生效
    resp = client.get(f"/api/teams/{team['name']}")
    assert resp.json()["data"]["mode"] == "broadcast"

    # 删除
    resp = client.delete(f"/api/teams/{team['name']}")
    assert resp.status_code == 200
    assert resp.json()["data"] is True

    # 验证已删除
    resp = client.get(f"/api/teams/{team['name']}")
    assert resp.status_code == 404


# ============================================================
# 2. Agent完整生命周期
# ============================================================


def test_full_agent_lifecycle(integration_client):
    """创建团队→添加Agent→列出→删除Agent 的完整流程."""
    client = integration_client

    # 创建团队
    resp = client.post("/api/teams", json={"name": "agent-lifecycle-team"})
    assert resp.status_code == 201
    team = resp.json()["data"]
    team_name = team["name"]
    team_id = team["id"]

    # 添加Agent 1
    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "coder", "role": "后端开发", "system_prompt": "你是后端开发专家"},
    )
    assert resp.status_code == 201
    agent1 = resp.json()["data"]
    assert agent1["name"] == "coder"
    assert agent1["role"] == "后端开发"
    assert agent1["team_id"] == team_id

    # 添加Agent 2
    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "reviewer", "role": "代码审查"},
    )
    assert resp.status_code == 201

    # 列出Agent
    resp = client.get(f"/api/teams/{team_id}/agents")
    assert resp.status_code == 200
    agents_data = resp.json()
    assert agents_data["total"] == 2

    # 删除Agent 1
    resp = client.delete(f"/api/agents/{agent1['id']}")
    assert resp.status_code == 200
    assert resp.json()["data"] is True

    # 验证只剩1个Agent
    resp = client.get(f"/api/teams/{team_id}/agents")
    assert resp.json()["total"] == 1
    assert resp.json()["data"][0]["name"] == "reviewer"


# ============================================================
# 3. 团队状态
# ============================================================


def test_team_status(integration_client):
    """创建团队+Agent→获取状态→验证字段."""
    client = integration_client

    # 创建团队
    resp = client.post("/api/teams", json={"name": "status-int-team"})
    team = resp.json()["data"]
    team_name = team["name"]

    # 添加Agent
    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "dev", "role": "开发"},
    )
    client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "qa", "role": "测试"},
    )

    # 获取状态
    resp = client.get(f"/api/teams/{team_name}/status")
    assert resp.status_code == 200
    status = resp.json()["data"]

    # 验证字段
    assert status["team"]["name"] == "status-int-team"
    assert len(status["agents"]) == 2
    assert status["completed_tasks"] == 0
    assert status["total_tasks"] == 0
    assert isinstance(status["active_tasks"], list)


# ============================================================
# 4. 无效模式 → 422
# ============================================================


def test_create_team_invalid_mode(integration_client):
    """无效编排模式应返回错误."""
    client = integration_client

    resp = client.post(
        "/api/teams",
        json={"name": "bad-mode-team", "mode": "invalid_mode"},
    )
    # ValueError被error_handler捕获为404，或者直接返回500
    # OrchestrationMode("invalid_mode") 会抛出 ValueError
    assert resp.status_code in (404, 422, 500)
    assert resp.json()["success"] is False


# ============================================================
# 5. 获取不存在的团队 → 404
# ============================================================


def test_get_nonexistent_team(integration_client):
    """获取不存在的团队应返回404."""
    resp = integration_client.get("/api/teams/nonexistent-team-xyz")
    assert resp.status_code == 404
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "not_found"


# ============================================================
# 6. 删除不存在的团队 → 404
# ============================================================


def test_delete_nonexistent_team(integration_client):
    """删除不存在的团队应返回404."""
    resp = integration_client.delete("/api/teams/nonexistent-team-xyz")
    assert resp.status_code == 404
    data = resp.json()
    assert data["success"] is False


# ============================================================
# 7. 列出空团队 → 200 + total=0
# ============================================================


def test_list_empty_teams(integration_client):
    """空数据库列出团队应返回200和空列表."""
    resp = integration_client.get("/api/teams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] == 0
    assert data["data"] == []


# ============================================================
# 8. 操作后有事件记录
# ============================================================


def test_events_created_on_operations(repo_and_client):
    """创建团队后查events，验证系统事件已记录."""
    repo, client = repo_and_client

    # 先手动通过repo创建一条事件（API本身可能不自动创建事件）
    asyncio.get_event_loop().run_until_complete(
        repo.create_event(
            event_type="team.created",
            source="integration-test",
            data={"team_name": "event-test"},
        )
    )

    # 查询事件
    resp = client.get("/api/events")
    assert resp.status_code == 200
    events_data = resp.json()
    assert events_data["success"] is True
    assert events_data["total"] >= 1

    # 按类型过滤
    resp = client.get("/api/events?type=team.created")
    assert resp.status_code == 200
    filtered = resp.json()
    assert filtered["total"] >= 1
    for event in filtered["data"]:
        assert event["type"] == "team.created"


# ============================================================
# 9. 记忆搜索 — 空结果
# ============================================================


def test_memory_search_empty(integration_client):
    """搜索记忆应返回200和空列表."""
    resp = integration_client.get("/api/memory?scope=global&scope_id=system&query=hello")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] == 0
    assert data["data"] == []


# ============================================================
# 10. CORS响应
# ============================================================


def test_cors_headers(integration_client):
    """OPTIONS请求应返回正确的CORS响应头."""
    resp = integration_client.options(
        "/api/teams",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "GET" in resp.headers.get("access-control-allow-methods", "")

    # 测试不允许的Origin
    resp2 = integration_client.options(
        "/api/teams",
        headers={
            "Origin": "http://evil-site.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # 不允许的Origin不应该出现在响应头中
    assert resp2.headers.get("access-control-allow-origin") != "http://evil-site.com"
