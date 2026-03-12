"""AI Team OS — Reducer节点实现.

Reducer负责收集所有Agent的并行输出，使用LLM智能合并生成最终结果。
用于Broadcast编排模式中，替代Leader的综合节点。
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig


async def reducer_node(state: dict, config: RunnableConfig) -> dict:
    """收集所有Agent输出并智能合并为最终结果.

    读取 agent_outputs 和原始任务，调用LLM综合所有Agent的并行输出，
    生成最终的 final_result。

    Args:
        state: LangGraph状态字典。
        config: 运行时配置。

    Returns:
        状态更新字典，包含 final_result 和 messages。
    """
    configurable = config.get("configurable", {})
    llm_model = configurable.get("llm_model", "claude-opus-4-6")

    task = state.get("current_task", "")
    agent_outputs = state.get("agent_outputs", {})

    # 构建各Agent的输出摘要
    outputs_text = []
    for agent_name, output in agent_outputs.items():
        outputs_text.append(f"### {agent_name} 的输出:\n{output}")
    all_outputs = "\n\n".join(outputs_text) if outputs_text else "（无Agent输出）"

    system_prompt = (
        "你是一个结果合并器（Reducer），负责将多个Agent并行执行的结果合并为一份完整的最终输出。\n"
        "所有Agent收到了相同的任务并各自独立完成，现在需要你：\n"
        "1. 识别各Agent输出中的共同点和独特贡献\n"
        "2. 消除重复内容\n"
        "3. 整合不同视角和见解\n"
        "4. 生成一份综合、全面、连贯的最终结果\n\n"
        "直接输出合并后的最终结果，不要包含多余的说明。"
    )

    user_content = (
        f"## 原始任务\n{task}\n\n"
        f"## 各Agent的并行输出\n{all_outputs}"
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
