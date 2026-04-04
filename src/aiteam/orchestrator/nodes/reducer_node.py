"""AI Team OS — Reducer node implementation.

Reducer collects all Agent parallel outputs and uses LLM to intelligently merge
them into a final result. Used in Broadcast orchestration mode as the synthesis node.
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig


async def reducer_node(state: dict, config: RunnableConfig) -> dict:
    """Collect all Agent outputs and intelligently merge into final result.

    Reads agent_outputs and original task, calls LLM to synthesize all Agent
    parallel outputs into the final final_result.

    Args:
        state: LangGraph state dictionary.
        config: Runtime configuration.

    Returns:
        State update dict containing final_result and messages.
    """
    configurable = config.get("configurable", {})
    llm_model = configurable.get("llm_model", "claude-opus-4-6")

    task = state.get("current_task", "")
    agent_outputs = state.get("agent_outputs", {})

    # Build output summary for each Agent
    outputs_text = []
    for agent_name, output in agent_outputs.items():
        outputs_text.append(f"### {agent_name} 的Output:\n{output}")
    all_outputs = "\n\n".join(outputs_text) if outputs_text else "无Agent输出"

    system_prompt = (
        "你是一个结果合并器（Reducer），负责将多个Agent并行执行的结果合并为一份完整的最终Output。\n"
        "所有Agent收到了相同的任务并各自独立完成，现在需要你：\n"
        "1. 识别各AgentOutput中的共同点和独特贡献\n"
        "2. 消除重复内容\n"
        "3. 整合不同视角和见解\n"
        "4. 生成一份综合、全面、连贯的最终结果\n\n"
        "直接Output合并后的最终结果，不要包含多余的说明。"
    )

    user_content = f"## 原始任务\n{task}\n\n## 各Agent的并行Output\n{all_outputs}"

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
