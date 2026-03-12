"""AI Team OS — Human-in-the-Loop 审批节点 单元测试."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langgraph.types import Command

from aiteam.orchestrator.graphs.coordinate import build_coordinate_graph
from aiteam.orchestrator.nodes.approval_node import approval_node
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
    ]


# ================================================================
# approval_node 测试
# ================================================================


@pytest.mark.asyncio()
async def test_approval_node_approved() -> None:
    """审批通过时返回approved状态."""
    state = {
        "leader_plan": "1. researcher调研 2. developer实现",
    }

    # mock interrupt返回审批通过
    with patch("aiteam.orchestrator.nodes.approval_node.interrupt") as mock_interrupt:
        mock_interrupt.return_value = {"approved": True}
        result = await approval_node(state, config={})

    assert isinstance(result, dict)
    assert result["approval_status"] == "approved"


@pytest.mark.asyncio()
async def test_approval_node_rejected() -> None:
    """审批拒绝时返回Command跳转到END."""
    state = {
        "leader_plan": "1. researcher调研 2. developer实现",
    }

    # mock interrupt返回审批拒绝
    with patch("aiteam.orchestrator.nodes.approval_node.interrupt") as mock_interrupt:
        mock_interrupt.return_value = {"approved": False, "reason": "计划不合理"}
        result = await approval_node(state, config={})

    assert isinstance(result, Command)
    assert result.goto == "__end__"
    assert result.update["approval_status"] == "rejected"
    assert "计划不合理" in result.update["final_result"]


@pytest.mark.asyncio()
async def test_approval_node_rejected_no_reason() -> None:
    """审批拒绝但无原因时使用默认原因."""
    state = {"leader_plan": "测试计划"}

    with patch("aiteam.orchestrator.nodes.approval_node.interrupt") as mock_interrupt:
        mock_interrupt.return_value = {"approved": False}
        result = await approval_node(state, config={})

    assert isinstance(result, Command)
    assert "无原因" in result.update["final_result"]


@pytest.mark.asyncio()
async def test_approval_interrupt_value() -> None:
    """验证interrupt传递的数据格式正确."""
    state = {
        "leader_plan": "测试执行计划内容",
    }

    with patch("aiteam.orchestrator.nodes.approval_node.interrupt") as mock_interrupt:
        mock_interrupt.return_value = {"approved": True}
        await approval_node(state, config={})

    # 验证interrupt被调用时传递了正确的数据
    mock_interrupt.assert_called_once()
    interrupt_data = mock_interrupt.call_args[0][0]
    assert interrupt_data["type"] == "approval_request"
    assert interrupt_data["plan"] == "测试执行计划内容"
    assert interrupt_data["message"] == "请审批以下执行计划"


# ================================================================
# Coordinate图结构测试（带审批）
# ================================================================


def test_coordinate_graph_with_approval(sample_agents: list[Agent]) -> None:
    """带审批的图应包含approval节点."""
    graph = build_coordinate_graph(
        agents=sample_agents,
        memory_store=None,
        require_approval=True,
    )

    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "approval" in node_names
    assert "agent_researcher" in node_names
    assert "agent_developer" in node_names
    assert "leader_synthesize" in node_names


def test_coordinate_graph_without_approval(sample_agents: list[Agent]) -> None:
    """不带审批的图结构不变，无approval节点."""
    graph = build_coordinate_graph(
        agents=sample_agents,
        memory_store=None,
        require_approval=False,
    )

    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "approval" not in node_names
    assert "agent_researcher" in node_names
    assert "agent_developer" in node_names
    assert "leader_synthesize" in node_names


def test_coordinate_graph_with_approval_no_agents() -> None:
    """带审批但无Agent时，approval直接连到leader_synthesize."""
    graph = build_coordinate_graph(
        agents=[],
        memory_store=None,
        require_approval=True,
    )

    node_names = set(graph.nodes.keys())
    assert "leader_plan" in node_names
    assert "approval" in node_names
    assert "leader_synthesize" in node_names

    # 无Agent节点
    agent_nodes = [n for n in node_names if n.startswith("agent_")]
    assert len(agent_nodes) == 0


def test_coordinate_graph_with_approval_compiles(sample_agents: list[Agent]) -> None:
    """带审批的图可以成功编译."""
    graph = build_coordinate_graph(
        agents=sample_agents,
        memory_store=None,
        require_approval=True,
    )
    compiled = graph.compile()
    assert hasattr(compiled, "ainvoke")


def test_coordinate_graph_default_no_approval(sample_agents: list[Agent]) -> None:
    """默认不启用审批."""
    graph = build_coordinate_graph(agents=sample_agents, memory_store=None)
    node_names = set(graph.nodes.keys())
    assert "approval" not in node_names
