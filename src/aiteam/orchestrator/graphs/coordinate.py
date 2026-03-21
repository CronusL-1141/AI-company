"""AI Team OS — Coordinate orchestration mode StateGraph.

Coordinate mode flow:
  START -> leader_plan -> agent_execute(sequential) -> leader_synthesize -> END

Leader analyzes the task and creates a plan, Agents execute subtasks sequentially,
then Leader synthesizes the results.
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
    """State definition for Coordinate mode."""

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
    """Build the StateGraph for Coordinate mode.

    Flow (no approval): START -> leader_plan -> [agent_1 -> agent_2 -> ...] -> leader_synthesize -> END
    Flow (with approval): START -> leader_plan -> approval -> [agent_1 -> ...] -> leader_synthesize -> END

    Args:
        agents: List of Agents in the team.
        memory_store: Optional MemoryStore instance.
        llm_model: Default LLM model name.
        require_approval: Whether to insert a human approval node after Leader planning.

    Returns:
        Compiled StateGraph executable object.
    """
    graph = StateGraph(CoordinateState)

    # Add Leader planning node
    graph.add_node("leader_plan", leader_plan_node)

    # If approval required, add approval node
    if require_approval:
        graph.add_node("approval", approval_node)

    # Add execution node for each Agent
    agent_node_names = []
    for agent in agents:
        node_name = f"agent_{agent.name}"
        agent_node_fn = create_agent_node(agent, memory_store=memory_store)
        graph.add_node(node_name, agent_node_fn)
        agent_node_names.append(node_name)

    # Add Leader synthesis node
    graph.add_node("leader_synthesize", leader_synthesize_node)

    # Build edges: START -> leader_plan
    graph.add_edge(START, "leader_plan")

    if require_approval:
        graph.add_edge("leader_plan", "approval")

    if agent_node_names:
        # Connect to first Agent (from approval or leader_plan)
        source = "approval" if require_approval else "leader_plan"
        graph.add_edge(source, agent_node_names[0])

        # Chain Agents sequentially
        for i in range(len(agent_node_names) - 1):
            graph.add_edge(agent_node_names[i], agent_node_names[i + 1])

        # Last Agent -> leader_synthesize
        graph.add_edge(agent_node_names[-1], "leader_synthesize")
    else:
        # When no Agents, go directly to leader_synthesize
        source = "approval" if require_approval else "leader_plan"
        graph.add_edge(source, "leader_synthesize")

    # leader_synthesize → END
    graph.add_edge("leader_synthesize", END)

    return graph
