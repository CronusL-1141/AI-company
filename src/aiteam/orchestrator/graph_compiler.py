"""AI Team OS — Graph compiler.

Compiles the corresponding LangGraph StateGraph based on the team's orchestration mode.
Supports Coordinate and Broadcast modes.
"""

from __future__ import annotations

from typing import Any

from aiteam.orchestrator.graphs.broadcast import build_broadcast_graph
from aiteam.orchestrator.graphs.coordinate import build_coordinate_graph
from aiteam.types import Agent, OrchestrationMode, Team


def compile_graph(
    team: Team,
    agents: list[Agent],
    memory_store: Any | None = None,
    llm_model: str = "claude-opus-4-6",
) -> Any:
    """Compile the corresponding StateGraph based on team orchestration mode.

    Args:
        team: Team configuration.
        agents: List of Agents in the team.
        memory_store: Optional MemoryStore instance.
        llm_model: Default LLM model name.

    Returns:
        Compiled LangGraph executable object.

    Raises:
        NotImplementedError: When orchestration mode is not supported in current phase.
    """
    mode = team.mode

    if mode == OrchestrationMode.COORDINATE:
        require_approval = team.config.get("require_approval", False)
        graph = build_coordinate_graph(
            agents=agents,
            memory_store=memory_store,
            llm_model=llm_model,
            require_approval=require_approval,
        )
        checkpointer = None
        # Checkpointer needed for interrupt/resume when approval is enabled
        return graph.compile(checkpointer=checkpointer)

    if mode == OrchestrationMode.BROADCAST:
        graph = build_broadcast_graph(
            agents=agents,
            memory_store=memory_store,
            llm_model=llm_model,
        )
        return graph.compile()

    if mode == OrchestrationMode.ROUTE:
        raise NotImplementedError("Route模式将在M3阶段实现")

    if mode == OrchestrationMode.MEET:
        raise NotImplementedError("Meet模式将在M3阶段实现")

    raise ValueError(f"未知的编排模式: {mode}")
