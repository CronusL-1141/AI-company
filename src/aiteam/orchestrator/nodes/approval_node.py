"""AI Team OS — Human-in-the-Loop 审批节点.

在Leader计划后插入，暂停执行等待人工审批。
使用LangGraph的interrupt机制实现。
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, interrupt


async def approval_node(state: dict, config: RunnableConfig) -> dict | Command:
    """审批节点 — 暂停执行等待人工决策.

    此节点会：
    1. 提取leader_plan
    2. 调用interrupt()暂停图执行
    3. 等待外部resume时传入的审批决策
    4. 根据决策继续或中止

    Args:
        state: LangGraph状态字典。
        config: 运行时配置。

    Returns:
        状态更新字典（审批通过）或Command（审批拒绝，跳转到END）。
    """
    plan = state.get("leader_plan", "")

    # 使用LangGraph interrupt暂停，等待外部输入
    decision = interrupt({
        "type": "approval_request",
        "plan": plan,
        "message": "请审批以下执行计划",
    })

    # 外部resume时传入decision
    if decision.get("approved", False):
        return {"approval_status": "approved"}
    else:
        return Command(goto="__end__", update={
            "approval_status": "rejected",
            "final_result": f"任务被人工拒绝: {decision.get('reason', '无原因')}",
        })
