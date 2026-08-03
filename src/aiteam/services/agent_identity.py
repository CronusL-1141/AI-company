"""行身份 = 派工身份 —— ``agents`` 行的复用判据（生命周期审计 A-06）。

**为什么需要一条硬判据**：``SubagentStart`` 的去重梯子有三级是按**名字**匹配的，
命中即把新派工就地绑到旧行；随后 ``SubagentStop`` 按 transcript **覆写**该行的四层
token 与 ``tokens_measured_at``。于是「同名」这一个巧合就足以让一次新派工抹掉上一次
派工的账，而 token 账是**不可重建**的派生数据（transcript 早晚会被清，账没了就再也
算不回来）——正是 2026-08-01 容器队清理事故立下的那条规矩：删数据前先问"删了能不
能重建"。

**计账单位就是 transcript 文件**，而 transcript 的文件名是
``agent-<cc_agent_id>.jsonl``。所以「一行 = 一次派工 = 一份 transcript」是同一句话，
判据只能按 ``cc_tool_use_id`` 立，不能按名字立。同一件事在 workflow 路径上早就判过
了（``_register_workflow_subagent``：按 cc_agent_id 去重而非名字，否则一次 16 个
agent 的 run 会被折叠成一行）——这里只是把同一条纪律补到正路径上。

本模块是**纯判断**，不碰库、不碰文件，只依赖 ``aiteam.types``。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from aiteam.types import Agent

# 四层用量列。任一非零即视为"这一行留过账"。
TOKEN_LAYERS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def has_token_account(agent: Agent) -> bool:
    """这一行是否已经挂了 token 账。判据刻意取**宽**。

    ``tokens_measured_at`` 非空就算有账，即使四层全是 0 —— 测得 0 也是测量结果，
    ``no-data != zero`` 是本项目反复立过的口径。覆盖它等于把"测过且为 0"改写成
    "测过且是别人的数"，两种错都不能犯。
    """
    if getattr(agent, "tokens_measured_at", None) is not None:
        return True
    return any(int(getattr(agent, layer, None) or 0) != 0 for layer in TOKEN_LAYERS)


def _sort_key(agent: Agent) -> tuple[datetime, str]:
    created = getattr(agent, "created_at", None) or _EPOCH
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (created, agent.id)


def pick_reusable_row(candidates: Iterable[Agent], cc_agent_id: str) -> Agent | None:
    """从同名候选行里挑一行给这次派工复用；挑不出就返回 None（调用方新建行）。

    复用**只有两种**合法情形，其余一律新建：

    1. **同一次派工**：候选行的 ``cc_tool_use_id`` 恰是本次的 cc id。重复
       ``SubagentStart``、续跑、同一份 transcript 再测一次都走这里，覆写自己的账
       是正确的。
    2. **从未绑过任何派工且没有账**：``cc_tool_use_id`` 为空的空行，典型来源是 MCP
       预注册（``source="api"``）——它本来就是等着被 hook 认领的占位行。

    候选按 ``created_at`` 降序（同刻用 id 兜底）遍历，所以答案与入参顺序无关，也与
    SQL 的行返回顺序无关：**确定性是硬要求**，原实现的 ``limit(1)`` 无 ORDER BY 是
    靠运气。同时更"新"的空行更可能就是为这次派工刚建的那一行。

    ``cc_agent_id`` 为空时（payload 没带 agent_id）无从判定派工身份，此时只保留账目
    闸门：不挑已挂账的行，但允许挑已绑别的派工的空账行 —— 宁可保守复用，也不凭空
    造行。
    """
    ordered = sorted(candidates, key=_sort_key, reverse=True)

    if cc_agent_id:
        for agent in ordered:
            if (agent.cc_tool_use_id or "") == cc_agent_id:
                return agent

    for agent in ordered:
        if has_token_account(agent):
            continue
        bound = agent.cc_tool_use_id or ""
        if bound and cc_agent_id and bound != cc_agent_id:
            # 这一行是**另一次**派工的身份证，即使它还没记上账也不能顶替
            continue
        return agent
    return None
