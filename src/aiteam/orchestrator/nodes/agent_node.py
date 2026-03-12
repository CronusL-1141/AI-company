"""AI Team OS — Agent节点实现.

每个Agent在LangGraph中是一个节点，接收状态、调用LLM、返回输出。
通过工厂函数 create_agent_node 为每个Agent创建对应的节点函数。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from aiteam.types import Agent


def create_agent_node(
    agent_config: Agent,
    memory_store: Any | None = None,
) -> Callable[..., Coroutine[Any, Any, dict]]:
    """工厂函数，为指定Agent创建对应的LangGraph节点函数.

    Args:
        agent_config: Agent配置（包含name、role、system_prompt、model等）。
        memory_store: 可选的MemoryStore实例，用于注入记忆上下文。

    Returns:
        异步节点函数，签名为 (state, config) -> dict。
    """

    async def agent_node(state: dict, config: RunnableConfig) -> dict:
        """Agent执行分配的子任务.

        从 leader_plan 中提取自己的子任务，结合记忆上下文调用LLM，
        将输出写入 agent_outputs。

        Args:
            state: LangGraph状态字典。
            config: 运行时配置。

        Returns:
            状态更新字典，包含 agent_outputs 和 messages。
        """
        configurable = config.get("configurable", {})
        llm_model = configurable.get("llm_model", agent_config.model)

        task = state.get("current_task", "")
        leader_plan = state.get("leader_plan", "")

        # 构建系统提示词
        base_prompt = agent_config.system_prompt or f"你是一位{agent_config.role}。"
        system_parts = [base_prompt]

        # 注入记忆上下文（如果memory_store可用）
        if memory_store is not None:
            try:
                memory_context = await memory_store.get_context(
                    agent_id=agent_config.id,
                    task=task,
                )
                if memory_context:
                    system_parts.append(f"\n## 相关记忆\n{memory_context}")
            except Exception:
                # memory不可用时静默跳过
                pass

        system_content = "\n".join(system_parts)

        user_content = (
            f"## 团队任务\n{task}\n\n"
            f"## Leader的执行计划\n{leader_plan}\n\n"
            f"请根据计划中分配给你（{agent_config.name}，{agent_config.role}）的子任务，"
            f"完成你负责的部分。直接输出工作成果。"
        )

        llm = ChatAnthropic(model=llm_model)
        messages = [
            SystemMessage(content=system_content),
            HumanMessage(content=user_content),
        ]
        response = await llm.ainvoke(messages)

        # 合并到已有的 agent_outputs
        existing_outputs = dict(state.get("agent_outputs", {}))
        existing_outputs[agent_config.name] = response.content

        return {
            "agent_outputs": existing_outputs,
            "messages": [response],
        }

    # 设置函数名以便调试
    agent_node.__name__ = f"agent_{agent_config.name}"
    agent_node.__qualname__ = f"agent_{agent_config.name}"

    return agent_node
