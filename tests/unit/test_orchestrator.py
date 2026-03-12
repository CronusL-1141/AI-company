"""AI Team OS — Orchestrator 单元测试."""

from __future__ import annotations

import pytest

from aiteam.orchestrator.graph_compiler import compile_graph
from aiteam.orchestrator.graphs.coordinate import build_coordinate_graph
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    Agent,
    OrchestrationMode,
    TaskStatus,
    Team,
)


# ================================================================
# Fixtures
# ================================================================


@pytest.fixture()
def manager(db_repository: StorageRepository) -> TeamManager:
    """创建 TeamManager 实例（无 memory）."""
    return TeamManager(repository=db_repository, memory=None)


# ================================================================
# 团队管理
# ================================================================


async def test_create_team(manager: TeamManager) -> None:
    """创建团队并验证字段."""
    team = await manager.create_team("dev-team", mode="coordinate")
    assert team.name == "dev-team"
    assert team.mode == OrchestrationMode.COORDINATE
    assert team.id

    # 通过名称获取
    fetched = await manager.get_team("dev-team")
    assert fetched.id == team.id


async def test_create_team_invalid_mode(manager: TeamManager) -> None:
    """无效的编排模式应抛出 ValueError."""
    with pytest.raises(ValueError):
        await manager.create_team("bad-team", mode="invalid_mode")


async def test_list_teams(manager: TeamManager) -> None:
    """列出所有团队."""
    await manager.create_team("team-a")
    await manager.create_team("team-b")
    teams = await manager.list_teams()
    names = {t.name for t in teams}
    assert "team-a" in names
    assert "team-b" in names


async def test_delete_team(manager: TeamManager) -> None:
    """删除团队."""
    await manager.create_team("to-delete")
    result = await manager.delete_team("to-delete")
    assert result is True

    with pytest.raises(ValueError, match="不存在"):
        await manager.get_team("to-delete")


async def test_get_team_not_found(manager: TeamManager) -> None:
    """获取不存在的团队应抛出 ValueError."""
    with pytest.raises(ValueError, match="不存在"):
        await manager.get_team("nonexistent")


async def test_set_mode(manager: TeamManager) -> None:
    """切换团队编排模式."""
    await manager.create_team("mode-team", mode="coordinate")
    updated = await manager.set_mode("mode-team", "broadcast")
    assert updated.mode == "broadcast" or updated.mode == OrchestrationMode.BROADCAST


async def test_set_mode_invalid(manager: TeamManager) -> None:
    """设置无效编排模式应抛出 ValueError."""
    await manager.create_team("mode-team2")
    with pytest.raises(ValueError):
        await manager.set_mode("mode-team2", "nonexistent_mode")


# ================================================================
# Agent 管理
# ================================================================


async def test_add_agent(manager: TeamManager) -> None:
    """向团队添加Agent并验证字段."""
    await manager.create_team("agent-team")
    agent = await manager.add_agent(
        team_name="agent-team",
        name="dev-1",
        role="后端开发",
        system_prompt="你是一位后端开发工程师。",
        model="claude-opus-4-6",
    )
    assert agent.name == "dev-1"
    assert agent.role == "后端开发"
    assert agent.system_prompt == "你是一位后端开发工程师。"
    assert agent.model == "claude-opus-4-6"


async def test_list_agents(manager: TeamManager) -> None:
    """列出团队中的所有Agent."""
    await manager.create_team("list-team")
    await manager.add_agent("list-team", "agent-a", "研究员")
    await manager.add_agent("list-team", "agent-b", "开发者")

    agents = await manager.list_agents("list-team")
    assert len(agents) == 2
    names = {a.name for a in agents}
    assert names == {"agent-a", "agent-b"}


async def test_remove_agent(manager: TeamManager) -> None:
    """从团队移除Agent."""
    await manager.create_team("remove-team")
    await manager.add_agent("remove-team", "to-remove", "测试角色")

    result = await manager.remove_agent("remove-team", "to-remove")
    assert result is True

    agents = await manager.list_agents("remove-team")
    assert len(agents) == 0


async def test_remove_agent_not_found(manager: TeamManager) -> None:
    """移除不存在的Agent应抛出 ValueError."""
    await manager.create_team("remove-team2")
    with pytest.raises(ValueError, match="不存在"):
        await manager.remove_agent("remove-team2", "ghost-agent")


# ================================================================
# 状态查询
# ================================================================


async def test_get_status(manager: TeamManager) -> None:
    """获取团队状态摘要."""
    await manager.create_team("status-team")
    await manager.add_agent("status-team", "worker-1", "开发者")
    await manager.add_agent("status-team", "worker-2", "测试员")

    status = await manager.get_status("status-team")
    assert status.team.name == "status-team"
    assert len(status.agents) == 2
    assert status.total_tasks == 0
    assert status.completed_tasks == 0


async def test_get_status_no_team(manager: TeamManager) -> None:
    """没有团队时获取状态应抛出 ValueError."""
    with pytest.raises(ValueError, match="没有可用的团队"):
        await manager.get_status(None)


# ================================================================
# Graph 编译
# ================================================================


async def test_compile_coordinate_graph(manager: TeamManager) -> None:
    """编译Coordinate图并验证图结构."""
    agents = [
        Agent(team_id="t1", name="researcher", role="研究员"),
        Agent(team_id="t1", name="developer", role="开发者"),
    ]
    graph = build_coordinate_graph(agents=agents, memory_store=None)

    # 验证节点存在
    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "agent_researcher" in node_names
    assert "agent_developer" in node_names
    assert "leader_synthesize" in node_names


async def test_compile_coordinate_graph_no_agents() -> None:
    """无Agent时图应直接从 leader_plan 连到 leader_synthesize."""
    graph = build_coordinate_graph(agents=[], memory_store=None)
    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "leader_synthesize" in node_names
    # 无Agent节点
    agent_nodes = [n for n in node_names if n.startswith("agent_")]
    assert len(agent_nodes) == 0


async def test_compile_coordinate_graph_via_compiler() -> None:
    """通过 compile_graph 编译Coordinate模式."""
    team = Team(name="test-team", mode=OrchestrationMode.COORDINATE)
    agents = [Agent(team_id=team.id, name="worker", role="工程师")]
    compiled = compile_graph(team=team, agents=agents)
    # compiled 应该是可调用的（CompiledGraph）
    assert hasattr(compiled, "ainvoke")


async def test_compile_unsupported_mode() -> None:
    """不支持的编排模式应抛出 NotImplementedError."""
    agents = [Agent(team_id="t1", name="worker", role="工程师")]

    # Broadcast已在M2实现，应正常编译
    team_broadcast = Team(name="t", mode=OrchestrationMode.BROADCAST)
    compiled = compile_graph(team=team_broadcast, agents=agents)
    assert hasattr(compiled, "ainvoke")

    team_route = Team(name="t", mode=OrchestrationMode.ROUTE)
    with pytest.raises(NotImplementedError, match="Route"):
        compile_graph(team=team_route, agents=agents)

    team_meet = Team(name="t", mode=OrchestrationMode.MEET)
    with pytest.raises(NotImplementedError, match="Meet"):
        compile_graph(team=team_meet, agents=agents)
