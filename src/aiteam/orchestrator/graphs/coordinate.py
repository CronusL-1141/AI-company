"""AI Team OS — Coordinate编排模式 StateGraph.

Coordinate模式流程:
  START → leader_plan → agent_execute(逐个) → leader_synthesize → END

Leader分析任务并制定计划，各Agent按顺序执行子任务，最后Leader综合结果。
"""

from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from aiteam.orchestrator.nodes.agent_node import create_agent_node
from aiteam.orchestrator.nodes.approval_node import approval_node
from aiteam.orchestrator.nodes.leader_node import (
    leader_plan_node,
    leader_synthesize_node,
)
from aiteam.types import Agent


class CoordinateState(TypedDict):
    """Coordinate模式的状态定义."""

    team_id: str
    current_task: str
    messages: Annotated[list[BaseMessage], add_messages]
    agent_outputs: dict[str, str]
    leader_plan: str | None
    final_result: str | None
    approval_status: str | None


def build_coordinate_graph(
    agents: list[Agent],
    memory_store: Any | None = None,
    llm_model: str = "claude-opus-4-6",
    require_approval: bool = False,
) -> StateGraph:
    """构建Coordinate模式的StateGraph.

    流程（无审批）: START → leader_plan → [agent_1 → agent_2 → ...] → leader_synthesize → END
    流程（有审批）: START → leader_plan → approval → [agent_1 → ...] → leader_synthesize → END

    Args:
        agents: 团队中的Agent列表。
        memory_store: 可选的MemoryStore实例。
        llm_model: 默认LLM模型名。
        require_approval: 是否在Leader计划后插入人工审批节点。

    Returns:
        已编译的StateGraph可执行对象。
    """
    graph = StateGraph(CoordinateState)

    # 添加Leader规划节点
    graph.add_node("leader_plan", leader_plan_node)

    # 如果需要审批，添加审批节点
    if require_approval:
        graph.add_node("approval", approval_node)

    # 为每个Agent添加执行节点
    agent_node_names = []
    for agent in agents:
        node_name = f"agent_{agent.name}"
        agent_node_fn = create_agent_node(agent, memory_store=memory_store)
        graph.add_node(node_name, agent_node_fn)
        agent_node_names.append(node_name)

    # 添加Leader综合节点
    graph.add_node("leader_synthesize", leader_synthesize_node)

    # 构建边: START → leader_plan
    graph.add_edge(START, "leader_plan")

    # leader_plan之后的下一个节点取决于是否需要审批
    post_plan_node = "approval" if require_approval else None

    if require_approval:
        graph.add_edge("leader_plan", "approval")

    if agent_node_names:
        # 连接到第一个Agent（从approval或leader_plan）
        source = "approval" if require_approval else "leader_plan"
        graph.add_edge(source, agent_node_names[0])

        # Agent之间顺序链接
        for i in range(len(agent_node_names) - 1):
            graph.add_edge(agent_node_names[i], agent_node_names[i + 1])

        # 最后一个Agent → leader_synthesize
        graph.add_edge(agent_node_names[-1], "leader_synthesize")
    else:
        # 没有Agent时，直接到leader_synthesize
        source = "approval" if require_approval else "leader_plan"
        graph.add_edge(source, "leader_synthesize")

    # leader_synthesize → END
    graph.add_edge("leader_synthesize", END)

    return graph
