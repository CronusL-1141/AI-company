"""AI Team OS — Human-in-the-Loop approval node.

Inserted after Leader planning; pauses execution to await human approval.
Implemented using LangGraph's interrupt mechanism.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, interrupt


async def approval_node(state: dict, config: RunnableConfig) -> dict | Command:
    """Approval node — pause execution to await human decision.

    This node:
    1. Extracts leader_plan
    2. Calls interrupt() to pause graph execution
    3. Waits for approval decision from external resume
    4. Continues or aborts based on decision

    Args:
        state: LangGraph state dictionary.
        config: Runtime configuration.

    Returns:
        State update dict (if approved) or Command (if rejected, jumps to END).
    """
    plan = state.get("leader_plan", "")

    # Use LangGraph interrupt to pause and wait for external input
    decision = interrupt({
        "type": "approval_request",
        "plan": plan,
        "message": "请审批以下执行计划",
    })

    # Decision passed in when externally resumed
    if decision.get("approved", False):
        return {"approval_status": "approved"}
    else:
        return Command(goto="__end__", update={
            "approval_status": "rejected",
            "final_result": f"任务被人工拒绝: {decision.get('reason', '无原因')}",
        })
