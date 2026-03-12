"""AI Team OS — Broadcast编排模式 StateGraph.

Broadcast模式流程:
  START → broadcast_node → [agent_1 ∥ agent_2 ∥ ...] → reducer_node → END

任务广播给所有Agent并行执行，Reducer智能合并所有输出。
"""

from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from aiteam.orchestrator.nodes.agent_node import create_agent_node
from aiteam.orchestrator.nodes.reducer_node import reducer_node
from aiteam.types import Agent


class BroadcastState(TypedDict):
    """Broadcast模式的状态定义."""

    team_id: str
    current_task: str
    messages: Annotated[list[BaseMessage], add_messages]
    agent_outputs: dict[str, str]
    final_result: str | None


def _broadcast_node(state: dict) -> dict:
    """将任务广播给所有Agent.

    在Broadcast模式中，每个Agent收到的子任务就是原始任务本身。
    此节点不修改状态，仅作为Fan-out的起点。

    Args:
        state: LangGraph状态字典。

    Returns:
        状态更新字典（初始化agent_outputs为空字典）。
    """
    return {
        "agent_outputs": {},
    }


def build_broadcast_graph(
    agents: list[Agent],
    memory_store: Any | None = None,
    llm_model: str = "claude-opus-4-6",
) -> StateGraph:
    """构建Broadcast模式的StateGraph.

    流程: START → broadcast_node → [agent_1 ∥ agent_2 ∥ ...] → reducer_node → END

    使用LangGraph的Fan-out模式：broadcast_node到每个agent_node都有边，
    所有Agent并行执行，最后汇聚到reducer_node合并结果。

    Args:
        agents: 团队中的Agent列表。
        memory_store: 可选的MemoryStore实例。
        llm_model: 默认LLM模型名。

    Returns:
        StateGraph实例（未编译）。
    """
    graph = StateGraph(BroadcastState)

    # 添加广播节点
    graph.add_node("broadcast_node", _broadcast_node)

    # 为每个Agent添加执行节点
    agent_node_names = []
    for agent in agents:
        node_name = f"agent_{agent.name}"
        agent_node_fn = create_agent_node(agent, memory_store=memory_store)
        graph.add_node(node_name, agent_node_fn)
        agent_node_names.append(node_name)

    # 添加Reducer合并节点
    graph.add_node("reducer_node", reducer_node)

    # 构建边: START → broadcast_node
    graph.add_edge(START, "broadcast_node")

    if agent_node_names:
        # Fan-out: broadcast_node → 每个Agent（并行）
        for node_name in agent_node_names:
            graph.add_edge("broadcast_node", node_name)

        # Fan-in: 每个Agent → reducer_node
        for node_name in agent_node_names:
            graph.add_edge(node_name, "reducer_node")
    else:
        # 没有Agent时，直接到Reducer（会输出"无Agent输出"）
        graph.add_edge("broadcast_node", "reducer_node")

    # reducer_node → END
    graph.add_edge("reducer_node", END)

    return graph
