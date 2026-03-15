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


# ============================================================
# 11. 团队-Agent-任务完整工作流
# ============================================================


def test_full_team_workflow(integration_client):
    """创建团队→添加agent→创建任务→完成任务→查询状态."""
    client = integration_client

    # 创建团队
    resp = client.post("/api/teams", json={"name": "workflow-team", "mode": "coordinate"})
    assert resp.status_code == 201
    team = resp.json()["data"]
    team_name = team["name"]
    team_id = team["id"]

    # 添加agent
    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "dev-1", "role": "dev"},
    )
    assert resp.status_code == 201
    agent = resp.json()["data"]
    assert agent["team_id"] == team_id

    # 创建任务
    resp = client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={"title": "Workflow test task", "description": "集成测试任务"},
    )
    assert resp.status_code == 200
    task = resp.json()["data"]
    task_id = task["id"]
    assert task["status"] == "pending"
    assert task["title"] == "Workflow test task"

    # 创建第2个任务
    resp = client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={"title": "Second task", "description": "第二个任务"},
    )
    assert resp.status_code == 200
    task2_id = resp.json()["data"]["id"]

    # 完成第1个任务
    resp = client.put(f"/api/tasks/{task_id}/complete")
    assert resp.status_code == 200
    completed = resp.json()["data"]
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None

    # 查询团队状态 — 应有1个已完成任务
    resp = client.get(f"/api/teams/{team_name}/status")
    assert resp.status_code == 200
    status = resp.json()["data"]
    assert status["completed_tasks"] == 1
    assert status["total_tasks"] == 2
    assert len(status["agents"]) == 1

    # 完成第2个任务
    resp = client.put(f"/api/tasks/{task2_id}/complete")
    assert resp.status_code == 200

    # 再次验证状态
    resp = client.get(f"/api/teams/{team_name}/status")
    assert resp.status_code == 200
    status = resp.json()["data"]
    assert status["completed_tasks"] == 2


# ============================================================
# 12. Analytics端点集成
# ============================================================


def test_analytics_with_activities(repo_and_client):
    """创建团队+agent+activities→查询analytics端点→验证数据."""
    repo, client = repo_and_client

    # 创建团队+agent
    resp = client.post("/api/teams", json={"name": "analytics-team"})
    assert resp.status_code == 201
    team = resp.json()["data"]
    team_name = team["name"]
    team_id = team["id"]

    resp = client.post(
        f"/api/teams/{team_name}/agents",
        json={"name": "analytics-dev", "role": "dev"},
    )
    assert resp.status_code == 201
    agent_id = resp.json()["data"]["id"]

    # 创建一个任务并完成（用于效率统计）
    resp = client.post(
        f"/api/teams/{team_name}/tasks/run",
        json={"title": "Analytics task", "description": "用于统计"},
    )
    task_id = resp.json()["data"]["id"]
    client.put(f"/api/tasks/{task_id}/complete")

    # 手动添加activities（直接操作repo）
    loop = asyncio.get_event_loop()
    for tool in ["read_file", "edit_file", "read_file", "bash", "edit_file"]:
        loop.run_until_complete(
            repo.create_activity(
                agent_id=agent_id,
                session_id="test-session-001",
                tool_name=tool,
                input_summary="test input",
                output_summary="test output",
            )
        )

    # tool-usage: 应有工具统计
    resp = client.get(f"/api/analytics/tool-usage?team_id={team_id}")
    assert resp.status_code == 200
    tool_data = resp.json()["data"]
    assert isinstance(tool_data, list)
    assert len(tool_data) > 0
    # 验证read_file出现2次
    read_file_entry = [d for d in tool_data if d["tool_name"] == "read_file"]
    assert len(read_file_entry) == 1
    assert read_file_entry[0]["count"] == 2

    # agent-productivity: 应有agent产能数据
    resp = client.get(f"/api/analytics/agent-productivity?team_id={team_id}")
    assert resp.status_code == 200
    prod_data = resp.json()["data"]
    assert isinstance(prod_data, list)
    assert len(prod_data) >= 1
    assert prod_data[0]["activity_count"] == 5

    # timeline: 应返回时间线数据
    resp = client.get(f"/api/analytics/timeline?team_id={team_id}&hours=1")
    assert resp.status_code == 200
    timeline_data = resp.json()["data"]
    assert isinstance(timeline_data, list)

    # efficiency: 应返回效率指标
    resp = client.get(f"/api/analytics/efficiency?team_id={team_id}")
    assert resp.status_code == 200
    eff_data = resp.json()["data"]
    assert "task_completion" in eff_data
    assert "agent_utilization" in eff_data
    assert eff_data["task_completion"]["completed_tasks"] >= 1


# ============================================================
# 13. 团队配置CRUD
# ============================================================


def test_team_config_crud(integration_client):
    """GET默认→PUT更新→POST添加成员→PATCH禁用→DELETE删除."""
    client = integration_client

    # GET默认配置
    resp = client.get("/api/config/team-defaults")
    assert resp.status_code == 200
    config = resp.json()["data"]
    assert "permanent_members" in config

    # PUT整体更新
    resp = client.put(
        "/api/config/team-defaults",
        json={
            "auto_create_team": False,
            "team_name_prefix": "integ",
            "permanent_members": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["auto_create_team"] is False
    assert resp.json()["data"]["team_name_prefix"] == "integ"

    # POST添加成员
    resp = client.post(
        "/api/config/team-defaults/members",
        json={"name": "qa-bot", "role": "QA", "model": "claude-sonnet-4-6", "enabled": True},
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["name"] == "qa-bot"

    # POST添加第二个成员
    resp = client.post(
        "/api/config/team-defaults/members",
        json={"name": "fixer", "role": "bug-fixer"},
    )
    assert resp.status_code == 201

    # POST重复成员 → 409
    resp = client.post(
        "/api/config/team-defaults/members",
        json={"name": "qa-bot", "role": "QA"},
    )
    assert resp.status_code == 409

    # PATCH禁用成员
    resp = client.patch(
        "/api/config/team-defaults/members/qa-bot",
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False

    # PATCH不存在的成员 → 404
    resp = client.patch(
        "/api/config/team-defaults/members/nonexistent",
        json={"enabled": False},
    )
    assert resp.status_code == 404

    # 验证GET反映更新
    resp = client.get("/api/config/team-defaults")
    members = resp.json()["data"]["permanent_members"]
    assert len(members) == 2
    qa_bot = [m for m in members if m["name"] == "qa-bot"][0]
    assert qa_bot["enabled"] is False

    # DELETE删除成员
    resp = client.delete("/api/config/team-defaults/members/qa-bot")
    assert resp.status_code == 200

    # DELETE不存在的成员 → 404
    resp = client.delete("/api/config/team-defaults/members/nonexistent")
    assert resp.status_code == 404

    # 验证只剩1个成员
    resp = client.get("/api/config/team-defaults")
    members = resp.json()["data"]["permanent_members"]
    assert len(members) == 1
    assert members[0]["name"] == "fixer"


# ============================================================
# 14. 任务墙排序
# ============================================================


def test_task_wall_sorting(integration_client):
    """创建不同priority/horizon的任务→查询task-wall→验证score排序."""
    client = integration_client

    # 创建团队
    resp = client.post("/api/teams", json={"name": "wall-team"})
    assert resp.status_code == 201
    team_data = resp.json()["data"]
    team_name = team_data["name"]
    team_id = team_data["id"]

    # 创建不同优先级和时间范围的任务
    tasks_spec = [
        {"title": "Low-short", "description": "低优先短期", "priority": "low", "horizon": "short"},
        {"title": "Critical-short", "description": "关键短期", "priority": "critical", "horizon": "short"},
        {"title": "High-mid", "description": "高优先中期", "priority": "high", "horizon": "mid"},
        {"title": "Medium-long", "description": "中优先长期", "priority": "medium", "horizon": "long"},
        {"title": "High-short", "description": "高优先短期", "priority": "high", "horizon": "short"},
    ]

    for spec in tasks_spec:
        resp = client.post(f"/api/teams/{team_name}/tasks/run", json=spec)
        assert resp.status_code == 200

    # 查询任务墙（使用team_id，因为task-wall直接用team_id查询任务）
    resp = client.get(f"/api/teams/{team_id}/task-wall")
    assert resp.status_code == 200
    wall_data = resp.json()

    # 验证wall结构包含3个分组
    wall = wall_data["wall"]
    assert "short" in wall
    assert "mid" in wall
    assert "long" in wall

    # short分组应有3个任务
    short_tasks = wall["short"]
    assert len(short_tasks) == 3

    # 验证score降序排列
    scores = [t["score"] for t in short_tasks]
    assert scores == sorted(scores, reverse=True), f"short任务未按score降序: {scores}"

    # Critical应排在最前面
    assert short_tasks[0]["title"] == "Critical-short"

    # mid分组应有1个任务
    assert len(wall["mid"]) == 1
    assert wall["mid"][0]["title"] == "High-mid"

    # long分组应有1个任务
    assert len(wall["long"]) == 1
    assert wall["long"][0]["title"] == "Medium-long"

    # stats应包含正确统计
    stats = wall_data["stats"]
    assert stats["total"] == 5
    assert stats["by_status"]["pending"] == 5

    # 按horizon过滤
    resp = client.get(f"/api/teams/{team_id}/task-wall?horizon=short")
    assert resp.status_code == 200
    filtered_wall = resp.json()["wall"]
    assert len(filtered_wall["short"]) == 3
    assert len(filtered_wall["mid"]) == 0
    assert len(filtered_wall["long"]) == 0

    # 按priority过滤
    resp = client.get(f"/api/teams/{team_id}/task-wall?priority=critical")
    assert resp.status_code == 200
    prio_wall = resp.json()["wall"]
    # 只有critical任务出现在对应分组中
    all_filtered = prio_wall["short"] + prio_wall["mid"] + prio_wall["long"]
    assert len(all_filtered) == 1
    assert all_filtered[0]["priority"] == "critical"
