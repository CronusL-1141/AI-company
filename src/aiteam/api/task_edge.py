"""``agent --worked_on--> task`` 边的寄生写入 —— token 用量归因 v1 §2.4。

**为什么不给 agents 加一个 task_id 列**：``SubagentStart`` 的 payload 里只有
agent_id / agent_type / session_id / cc_team_name，没有 prompt、没有任何任务标识。
在 agent 出生的那一刻，OS 无从得知它是为哪个 task 干活的。那一列会长期为 NULL，
而 NULL 在归因语境里极易被读成"这段工作没花 token" —— 加一个填不上的列比不加更糟
（§2.3）。

**为什么不新增一个"绑定 agent 与 task"的工具**：22 天内 ``task_memo_add`` 被调
711 次、``task_update`` 179 次，而"任何需要人主动多调一次工具的设计都很难存活"已被
反复验证。所以这里的做法是：在**已经必然发生**的记账行为里顺手把边写下来 —— 这三个
工具的调用参数里天然同时带着 ``task_id`` 与 ``author``（§2.4）。

边的语义边界（写在这里是因为它极易被读成另一个意思）：这条边说的是"这个 agent 在
这个 task 上留过账"，**不是**"这个 agent 的全部 token 都属于这个 task"。一个 agent
跨多个 task 时，归因推导器如实计为 task 级未归因，不做平均分摊（见
``repository._single_worked_on_agents_subquery``）。
"""

from __future__ import annotations

import logging

from fastapi import Request

from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_SOURCE,
    AGENT_TASK_LINK_TO_KIND,
    AGENT_TASK_LINK_TYPE,
    KnowledgeLink,
)

logger = logging.getLogger(__name__)

# MCP 侧把当前 CC 会话 id 放在这个头里（见 mcp/_base._api_call）。没有它就解析不出
# 域，也就不写边 —— 见 resolve_author_agent_id 的文档。
SESSION_HEADER = "X-CC-Session-Id"


def session_id_from_request(request: Request | None) -> str:
    """从请求头取当前 CC 会话 id；取不到返回空串（调用方据此放弃写边）。"""
    if request is None:
        return ""
    return (request.headers.get(SESSION_HEADER) or "").strip()


async def resolve_author_agent_id(
    repo: StorageRepository, author: str, session_id: str
) -> str | None:
    """把记账动作里的 ``author`` 名解析成一个 agent 行 id；解析不出返回 None。

    两档，都**限定在当前会话的域内**，域内再按 ``created_at`` 取最新：

    档① ``agents.session_id`` 命中 —— 身份的权威来源。阶段 1 由 transcript 路径
        回填后，子 agent 行 78.5% 有值。
    档② 该会话名下的队（``teams.config.owner_session_id`` / ``session-<sid8>``）里的
        同名行。这一档不是冗余：Leader 行的 ``session_id`` 实测 0/117 全为 NULL
        （历史上被启动清扫抹过，阶段 1 修了根因但旧行补不回来），而 Leader 恰好是
        记账动作最频繁的作者，缺了这一档它一条边都建不出来。

    **解析不到就不写**，绝不退回"全表按名字找最新"。这不是保守，是因为那样做必然
    绑错：``name`` 在 agents 表里不唯一，实测 ``Leader`` 一个名字 117 行横跨 79 支队、
    时间跨度三周。跨时重名错绑的边写进去照样查得到、不报错，只会让 task 级归因悄悄
    指向另一个会话的另一个 agent —— 一个永远不会有人发现的错误。宁可少一条边。
    """
    author = (author or "").strip()
    if not author or not session_id:
        return None

    agent = await repo.find_latest_agent_by_name(author, session_id=session_id)
    if agent is None:
        team_ids = await repo.find_teams_by_owner_session(session_id)
        if not team_ids:
            return None
        agent = await repo.find_latest_agent_by_name(author, team_ids=team_ids)
    return agent.id if agent is not None else None


async def record_worked_on(
    repo: StorageRepository,
    *,
    task_id: str,
    author: str,
    session_id: str,
    project_id: str = "",
    origin: str = "",
) -> str | None:
    """记一条 ``agent --worked_on--> task`` 边；返回绑定到的 agent id，没绑上返回 None。

    幂等：``knowledge_links`` 的五元组 UNIQUE 使同一 (agent, task) 只留一条边，重复
    记账不会堆积。``origin`` 只进 context 供事后审计（是哪个记账动作带出来的这条边）。

    调用方一律 best-effort 包裹：**归因是观测，观测坏了不该拖垮被观测的记账动作**。
    """
    if not task_id:
        return None
    agent_id = await resolve_author_agent_id(repo, author, session_id)
    if agent_id is None:
        return None
    await repo.insert_knowledge_links([
        KnowledgeLink(
            from_kind=AGENT_TASK_LINK_FROM_KIND,
            from_id=agent_id,
            to_kind=AGENT_TASK_LINK_TO_KIND,
            to_id=task_id,
            link_type=AGENT_TASK_LINK_TYPE,
            context=f"author={author} via={origin}" if origin else f"author={author}",
            link_source=AGENT_TASK_LINK_SOURCE,
            project_id=project_id or "",
        )
    ])
    return agent_id


async def try_record_worked_on(
    repo: StorageRepository,
    *,
    task_id: str,
    author: str,
    request: Request | None,
    project_id: str = "",
    origin: str = "",
) -> None:
    """路由层调用的 best-effort 包装 —— 任何失败只记日志，绝不影响记账动作本身。"""
    try:
        agent_id = await record_worked_on(
            repo,
            task_id=task_id,
            author=author,
            session_id=session_id_from_request(request),
            project_id=project_id,
            origin=origin,
        )
        if agent_id is None:
            logger.debug(
                "worked_on edge skipped: author=%r unresolvable in session domain (%s)",
                author,
                origin,
            )
    except Exception:  # noqa: BLE001 — 观测失败不该拖垮被观测的动作
        logger.warning("worked_on edge write failed (%s)", origin, exc_info=True)
