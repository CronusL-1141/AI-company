"""AI Team OS — Leader node implementation.

Leader analyzes tasks, creates work distribution plans, and synthesizes Agent outputs
into final results.
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig


async def leader_plan_node(state: dict, config: RunnableConfig) -> dict:
    """Leader analyzes the task and creates an execution plan.

    Reads current_task and agents config, calls LLM to generate a work distribution plan,
    and writes it to the leader_plan field.

    Args:
        state: LangGraph state dictionary.
        config: Runtime config containing configurable.agents, configurable.llm_model.

    Returns:
        State update dict containing leader_plan and messages.
    """
    configurable = config.get("configurable", {})
    agents = configurable.get("agents", [])
    llm_model = configurable.get("llm_model", "claude-opus-4-6")

    task = state.get("current_task", "")

    # Build Agent info descriptions
    agent_descriptions = []
    for agent in agents:
        desc = f"- {agent.name}（角色: {agent.role}）"
        if agent.system_prompt:
            desc += f" — {agent.system_prompt[:100]}"
        agent_descriptions.append(desc)
    agents_info = "\n".join(agent_descriptions) if agent_descriptions else "（无可用Agent）"

    system_prompt = (
        "你是一个团队的Leader，负责分析任务并制定执行计划。\n"
        "你需要将任务分解为子任务，并分配给合适的团队成员。\n\n"
        "你的团队成员:\n"
        f"{agents_info}\n\n"
        "请Output一个清晰的执行计划，格式如下:\n"
        "1. 为每个Agent分配具体的子任务\n"
        "2. 说明每个子任务的目标和要求\n"
        "3. 如果某些Agent不需要参与，可以跳过\n\n"
        "直接Output计划内容，不要包含多余的说明。"
    )

    llm = ChatAnthropic(model=llm_model)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"任务: {task}"),
    ]
    response = await llm.ainvoke(messages)

    return {
        "leader_plan": response.content,
        "messages": [response],
    }


async def leader_synthesize_node(state: dict, config: RunnableConfig) -> dict:
    """Leader synthesizes all Agent outputs into the final result.

    Reads agent_outputs and original task, calls LLM for comprehensive analysis,
    and generates the final final_result.

    Args:
        state: LangGraph state dictionary.
        config: Runtime configuration.

    Returns:
        State update dict containing final_result and messages.
    """
    configurable = config.get("configurable", {})
    llm_model = configurable.get("llm_model", "claude-opus-4-6")

    task = state.get("current_task", "")
    leader_plan = state.get("leader_plan", "")
    agent_outputs = state.get("agent_outputs", {})

    # Build output summary for each Agent
    outputs_text = []
    for agent_name, output in agent_outputs.items():
        outputs_text.append(f"### {agent_name} 的Output:\n{output}")
    all_outputs = "\n\n".join(outputs_text) if outputs_text else "（无AgentOutput）"

    system_prompt = (
        "你是一个团队的Leader，负责综合各团队成员的工作成果，生成最终结果。\n"
        "请基于原始任务和各成员的Output，生成一份完整、连贯的最终结果。\n"
        "确保结果准确、全面，并融合各成员的贡献。\n"
        "直接Output最终结果，不要包含多余的说明。"
    )

    user_content = (
        f"## 原始任务\n{task}\n\n"
        f"## 执行计划\n{leader_plan}\n\n"
        f"## 各成员Output\n{all_outputs}"
    )

    llm = ChatAnthropic(model=llm_model)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]
    response = await llm.ainvoke(messages)

    return {
        "final_result": response.content,
        "messages": [response],
    }
