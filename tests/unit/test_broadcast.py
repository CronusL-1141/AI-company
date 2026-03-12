"""AI Team OS — Broadcast编排模式 单元测试."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from aiteam.orchestrator.graph_compiler import compile_graph
from aiteam.orchestrator.graphs.broadcast import (
    BroadcastState,
    _broadcast_node,
    build_broadcast_graph,
)
from aiteam.orchestrator.nodes.reducer_node import reducer_node
from aiteam.types import Agent, OrchestrationMode, Team


# ================================================================
# Fixtures
# ================================================================


@pytest.fixture()
def sample_agents() -> list[Agent]:
    """创建测试用Agent列表."""
    return [
        Agent(team_id="t1", name="researcher", role="研究员"),
        Agent(team_id="t1", name="developer", role="开发者"),
        Agent(team_id="t1", name="reviewer", role="审查员"),
    ]


@pytest.fixture()
def single_agent() -> list[Agent]:
    """创建单Agent列表."""
    return [Agent(team_id="t1", name="worker", role="工程师")]


# ================================================================
# Graph结构测试
# ================================================================


def test_build_broadcast_graph_structure(sample_agents: list[Agent]) -> None:
    """验证Broadcast图的节点和边结构."""
    graph = build_broadcast_graph(agents=sample_agents, memory_store=None)

    # 验证所有节点存在
    node_names = set(graph.nodes.keys())
    assert "broadcast_node" in node_names
    assert "agent_researcher" in node_names
    assert "agent_developer" in node_names
    assert "agent_reviewer" in node_names
    assert "reducer_node" in node_names


def test_build_broadcast_graph_no_agents() -> None:
    """无Agent时图应从broadcast_node直接到reducer_node."""
    graph = build_broadcast_graph(agents=[], memory_store=None)

    node_names = set(graph.nodes.keys())
    assert "broadcast_node" in node_names
    assert "reducer_node" in node_names

    # 不应有Agent节点
    agent_nodes = [n for n in node_names if n.startswith("agent_")]
    assert len(agent_nodes) == 0


def test_build_broadcast_graph_single_agent(single_agent: list[Agent]) -> None:
    """单Agent时图结构正确."""
    graph = build_broadcast_graph(agents=single_agent, memory_store=None)

    node_names = set(graph.nodes.keys())
    assert "broadcast_node" in node_names
    assert "agent_worker" in node_names
    assert "reducer_node" in node_names

    # 只有一个Agent节点
    agent_nodes = [n for n in node_names if n.startswith("agent_")]
    assert len(agent_nodes) == 1


def test_build_broadcast_graph_compiles(sample_agents: list[Agent]) -> None:
    """验证Broadcast图可以成功编译."""
    graph = build_broadcast_graph(agents=sample_agents, memory_store=None)
    compiled = graph.compile()
    assert hasattr(compiled, "ainvoke")


# ================================================================
# broadcast_node 测试
# ================================================================


def test_broadcast_node() -> None:
    """验证broadcast_node正确初始化agent_outputs."""
    state = {
        "team_id": "t1",
        "current_task": "分析市场数据",
        "messages": [],
        "agent_outputs": {"old_key": "old_value"},
        "final_result": None,
    }

    result = _broadcast_node(state)

    # 应初始化为空字典（清除之前的输出）
    assert result["agent_outputs"] == {}


def test_broadcast_node_preserves_task() -> None:
    """验证broadcast_node不修改current_task."""
    state = {
        "current_task": "测试任务",
        "agent_outputs": {},
    }

    result = _broadcast_node(state)

    # 不应包含current_task的更新
    assert "current_task" not in result


# ================================================================
# reducer_node 测试
# ================================================================


@pytest.mark.asyncio()
async def test_reducer_node() -> None:
    """验证reducer_node正确调用LLM并合并结果."""
    mock_response = AIMessage(content="合并后的最终结果")

    state = {
        "current_task": "分析市场趋势",
        "agent_outputs": {
            "researcher": "市场调研结果...",
            "developer": "技术分析结果...",
        },
    }

    config = {"configurable": {"llm_model": "claude-opus-4-6"}}

    with patch("aiteam.orchestrator.nodes.reducer_node.ChatAnthropic") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = mock_response
        mock_llm_cls.return_value = mock_llm

        result = await reducer_node(state, config)

    assert result["final_result"] == "合并后的最终结果"
    assert len(result["messages"]) == 1
    assert result["messages"][0] == mock_response

    # 验证LLM调用参数中包含了所有Agent输出
    call_args = mock_llm.ainvoke.call_args[0][0]
    user_msg = call_args[1].content
    assert "researcher" in user_msg
    assert "developer" in user_msg


@pytest.mark.asyncio()
async def test_reducer_node_no_outputs() -> None:
    """验证reducer_node在无Agent输出时的行为."""
    mock_response = AIMessage(content="无内容可合并")

    state = {
        "current_task": "测试任务",
        "agent_outputs": {},
    }

    config = {"configurable": {}}

    with patch("aiteam.orchestrator.nodes.reducer_node.ChatAnthropic") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = mock_response
        mock_llm_cls.return_value = mock_llm

        result = await reducer_node(state, config)

    assert result["final_result"] == "无内容可合并"

    # 验证提示词中包含无输出提示
    call_args = mock_llm.ainvoke.call_args[0][0]
    user_msg = call_args[1].content
    assert "无Agent输出" in user_msg


# ================================================================
# compile_graph 集成测试
# ================================================================


def test_compile_graph_broadcast_mode(sample_agents: list[Agent]) -> None:
    """验证编译器正确选择Broadcast模式."""
    team = Team(name="broadcast-team", mode=OrchestrationMode.BROADCAST)
    compiled = compile_graph(team=team, agents=sample_agents)

    # compiled 应该是可调用的（CompiledGraph）
    assert hasattr(compiled, "ainvoke")


def test_compile_graph_broadcast_no_agents() -> None:
    """验证编译器在无Agent时也能编译Broadcast图."""
    team = Team(name="empty-team", mode=OrchestrationMode.BROADCAST)
    compiled = compile_graph(team=team, agents=[])
    assert hasattr(compiled, "ainvoke")


def test_compile_graph_coordinate_still_works() -> None:
    """验证Coordinate模式在添加Broadcast后仍然正常."""
    team = Team(name="coord-team", mode=OrchestrationMode.COORDINATE)
    agents = [Agent(team_id=team.id, name="worker", role="工程师")]
    compiled = compile_graph(team=team, agents=agents)
    assert hasattr(compiled, "ainvoke")
