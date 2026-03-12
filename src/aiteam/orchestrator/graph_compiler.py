"""AI Team OS — Graph编译器.

根据团队的编排模式编译对应的LangGraph StateGraph。
支持Coordinate和Broadcast模式。
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
    """根据团队编排模式编译对应的StateGraph.

    Args:
        team: 团队配置。
        agents: 团队中的Agent列表。
        memory_store: 可选的MemoryStore实例。
        llm_model: 默认LLM模型名。

    Returns:
        已编译的LangGraph可执行对象。

    Raises:
        NotImplementedError: 当编排模式在当前阶段不支持时。
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
        # 启用审批时需要checkpointer支持interrupt/resume
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
