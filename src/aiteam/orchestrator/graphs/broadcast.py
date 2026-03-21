"""AI Team OS — Broadcast orchestration mode StateGraph.

Broadcast mode flow:
  START -> broadcast_node -> [agent_1 || agent_2 || ...] -> reducer_node -> END

Task is broadcast to all Agents for parallel execution; Reducer intelligently merges all outputs.
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
    """State definition for Broadcast mode."""

    team_id: str
    current_task: str
    messages: Annotated[list[BaseMessage], add_messages]
    agent_outputs: dict[str, str]
    final_result: str | None


def _broadcast_node(state: dict) -> dict:
    """Broadcast the task to all Agents.

    In Broadcast mode, each Agent receives the original task as its subtask.
    This node does not modify state; it serves as the fan-out starting point.

    Args:
        state: LangGraph state dictionary.

    Returns:
        State update dict (initializes agent_outputs as empty dict).
    """
    return {
        "agent_outputs": {},
    }


def build_broadcast_graph(
    agents: list[Agent],
    memory_store: Any | None = None,
    llm_model: str = "claude-opus-4-6",
) -> StateGraph:
    """Build the StateGraph for Broadcast mode.

    Flow: START -> broadcast_node -> [agent_1 || agent_2 || ...] -> reducer_node -> END

    Uses LangGraph's fan-out pattern: broadcast_node has edges to each agent_node,
    all Agents execute in parallel, then converge at reducer_node to merge results.

    Args:
        agents: List of Agents in the team.
        memory_store: Optional MemoryStore instance.
        llm_model: Default LLM model name.

    Returns:
        StateGraph instance (not compiled).
    """
    graph = StateGraph(BroadcastState)

    # Add broadcast node
    graph.add_node("broadcast_node", _broadcast_node)

    # Add execution node for each Agent
    agent_node_names = []
    for agent in agents:
        node_name = f"agent_{agent.name}"
        agent_node_fn = create_agent_node(agent, memory_store=memory_store)
        graph.add_node(node_name, agent_node_fn)
        agent_node_names.append(node_name)

    # Add Reducer merge node
    graph.add_node("reducer_node", reducer_node)

    # Build edges: START -> broadcast_node
    graph.add_edge(START, "broadcast_node")

    if agent_node_names:
        # Fan-out: broadcast_node -> each Agent (parallel)
        for node_name in agent_node_names:
            graph.add_edge("broadcast_node", node_name)

        # Fan-in: each Agent -> reducer_node
        for node_name in agent_node_names:
            graph.add_edge(node_name, "reducer_node")
    else:
        # When no Agents, go directly to Reducer (will output "no Agent output")
        graph.add_edge("broadcast_node", "reducer_node")

    # reducer_node → END
    graph.add_edge("reducer_node", END)

    return graph
