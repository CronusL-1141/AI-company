"""agent→task 边的寄生写入单测 —— 归因 v1 §2.4。

本文件的重点全在**解析不到就不写**这条上。`agents.name` 不唯一是实测事实，不是
理论风险：生产库里 ``Leader`` 一个名字对应 117 行、横跨 79 支队、时间跨度三周。
一条跨时重名错绑的边写进去照样查得到、照样不报错，只会让 task 级归因悄悄指向另一
个会话的另一个 agent —— 一个永远不会有人发现的错误。所以宁可少一条边。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aiteam.api.task_edge import (
    SESSION_HEADER,
    record_worked_on,
    resolve_author_agent_id,
    session_id_from_request,
)
from aiteam.clock import utc_now
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import AgentModel, TeamModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_TYPE,
    AttributionScope,
    TokenMetric,
)

DB = "sqlite+aiosqlite://"
SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
OTHER_SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
async def repo():
    r = StorageRepository(db_url=DB)
    await r.init_db()
    yield r
    await close_db()


async def _agent(
    *,
    agent_id: str,
    name: str,
    session_id: str | None = None,
    team_id: str = "t1",
    created_at=None,
    role: str = "worker",
) -> None:
    async with get_session(DB) as session:
        session.add(
            AgentModel(
                id=agent_id,
                team_id=team_id,
                name=name,
                role=role,
                session_id=session_id,
                created_at=created_at or utc_now(),
            )
        )


async def _team(*, team_id: str, name: str, owner_session_id: str | None = None) -> None:
    config = {"kind": "session"}
    if owner_session_id:
        config["owner_session_id"] = owner_session_id
    async with get_session(DB) as session:
        session.add(TeamModel(id=team_id, name=name, config=config, created_at=utc_now()))


async def _edges(repo: StorageRepository, task_id: str) -> list:
    links = await repo.find_knowledge_links("task", task_id, direction="in")
    return [
        lk
        for lk in links
        if lk.from_kind == AGENT_TASK_LINK_FROM_KIND and lk.link_type == AGENT_TASK_LINK_TYPE
    ]


class TestAuthorResolutionIsDomainBound:
    @pytest.mark.asyncio
    async def test_resolves_within_current_session(self, repo):
        await _agent(agent_id="mine", name="backend-architect", session_id=SESSION)
        assert await resolve_author_agent_id(repo, "backend-architect", SESSION) == "mine"

    @pytest.mark.asyncio
    async def test_same_name_in_another_session_is_never_bound(self, repo):
        """Leader 审查点：防跨时重名错绑。

        另一个会话里三周前有个同名 agent，本会话里没有 —— 正确行为是**不写**，
        不是"找到了一个同名的就用它"。
        """
        await _agent(
            agent_id="stranger",
            name="backend-architect",
            session_id=OTHER_SESSION,
            team_id="t-other",
            created_at=utc_now() - timedelta(days=21),
        )
        assert await resolve_author_agent_id(repo, "backend-architect", SESSION) is None

    @pytest.mark.asyncio
    async def test_newer_in_domain_row_wins_over_older_in_domain_row(self, repo):
        """同一会话里同名行取 ``created_at`` 最新的那一个（agent 可能被重派多次）。"""
        await _agent(
            agent_id="old",
            name="worker",
            session_id=SESSION,
            created_at=utc_now() - timedelta(hours=5),
        )
        await _agent(agent_id="new", name="worker", session_id=SESSION, created_at=utc_now())
        assert await resolve_author_agent_id(repo, "worker", SESSION) == "new"

    @pytest.mark.asyncio
    async def test_older_in_domain_beats_newer_out_of_domain(self, repo):
        """域优先于新鲜度 —— 别的会话里刚建的同名行再新也不算数。"""
        await _agent(
            agent_id="mine-old",
            name="worker",
            session_id=SESSION,
            created_at=utc_now() - timedelta(days=1),
        )
        await _agent(
            agent_id="theirs-new",
            name="worker",
            session_id=OTHER_SESSION,
            team_id="t-other",
            created_at=utc_now(),
        )
        assert await resolve_author_agent_id(repo, "worker", SESSION) == "mine-old"

    @pytest.mark.asyncio
    async def test_team_fallback_covers_leader_rows_with_null_session(self, repo):
        """Leader 行的 ``session_id`` 实测 0/117 全为 NULL（被历史启动清扫抹过）。

        而 Leader 恰好是记账动作最频繁的作者 —— 没有这一档它一条边都建不出来。
        """
        await _team(team_id="t-mine", name="session-80d0cc5e", owner_session_id=SESSION)
        await _agent(
            agent_id="leader-row", name="Leader", session_id=None, team_id="t-mine", role="leader"
        )
        assert await resolve_author_agent_id(repo, "leader", SESSION) == "leader-row"

    @pytest.mark.asyncio
    async def test_team_fallback_does_not_reach_other_sessions_teams(self, repo):
        await _team(team_id="t-theirs", name="session-11111111", owner_session_id=OTHER_SESSION)
        await _agent(
            agent_id="their-leader", name="Leader", team_id="t-theirs", role="leader"
        )
        assert await resolve_author_agent_id(repo, "leader", SESSION) is None

    @pytest.mark.asyncio
    async def test_author_match_is_case_insensitive(self, repo):
        """memo 的 author 默认是 ``"leader"``，而 Leader 行的 name 是 ``"Leader"``。"""
        await _agent(agent_id="l", name="Leader", session_id=SESSION, role="leader")
        assert await resolve_author_agent_id(repo, "LEADER", SESSION) == "l"
        assert await resolve_author_agent_id(repo, "  leader  ", SESSION) == "l"

    @pytest.mark.asyncio
    async def test_no_session_means_no_binding(self, repo):
        """拿不到会话头就不写 —— 否则只能全表按名字找，那必然错绑。"""
        await _agent(agent_id="mine", name="worker", session_id=SESSION)
        assert await resolve_author_agent_id(repo, "worker", "") is None

    @pytest.mark.asyncio
    async def test_unknown_author_is_not_invented(self, repo):
        await _agent(agent_id="mine", name="backend-architect", session_id=SESSION)
        assert await resolve_author_agent_id(repo, "d3-p2-typelayer", SESSION) is None


class TestEdgeWriting:
    @pytest.mark.asyncio
    async def test_writes_one_edge_and_is_idempotent(self, repo):
        await _agent(agent_id="mine", name="w", session_id=SESSION)
        first = await record_worked_on(
            repo, task_id="T1", author="w", session_id=SESSION, origin="task_memo_add"
        )
        second = await record_worked_on(
            repo, task_id="T1", author="w", session_id=SESSION, origin="task_memo_add"
        )
        assert first == second == "mine"
        edges = await _edges(repo, "T1")
        assert len(edges) == 1
        assert edges[0].from_id == "mine"

    @pytest.mark.asyncio
    async def test_unresolvable_author_writes_nothing(self, repo):
        assert (
            await record_worked_on(repo, task_id="T1", author="ghost", session_id=SESSION) is None
        )
        assert await _edges(repo, "T1") == []

    @pytest.mark.asyncio
    async def test_edge_feeds_task_scope_attribution(self, repo):
        """写边 → 归因推导器立刻能按 task 聚合。两侧共用同一份 link 词汇。"""
        await _agent(agent_id="mine", name="w", session_id=SESSION)
        await record_worked_on(repo, task_id="T1", author="w", session_id=SESSION)
        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id="T1"
        )
        assert att.dispatches_total == 1

    @pytest.mark.asyncio
    async def test_second_task_makes_the_agent_unsplittable(self, repo):
        """一个 agent 在两个 task 上留过账 → 两边都如实计未归因，不平均分摊。"""
        await _agent(agent_id="mine", name="w", session_id=SESSION)
        await record_worked_on(repo, task_id="T1", author="w", session_id=SESSION)
        await record_worked_on(repo, task_id="T2", author="w", session_id=SESSION)
        for task_id in ("T1", "T2"):
            att = await repo.aggregate_token_attribution(
                metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id=task_id
            )
            assert att.dispatches_attributed == 0
            assert att.dispatches_total == 1


class TestSessionHeader:
    def test_reads_the_agreed_header(self):
        class _Req:
            headers = {SESSION_HEADER: "  abc  "}

        assert session_id_from_request(_Req()) == "abc"
        assert session_id_from_request(None) == ""

    def test_mcp_base_sends_it(self):
        """MCP 侧必须把会话 id 带上，否则服务端永远解析不出域、永远不写边。"""
        import inspect

        from aiteam.mcp import _base

        source = inspect.getsource(_base._api_call)
        assert SESSION_HEADER in source
        assert "_cc_session_id()" in source
