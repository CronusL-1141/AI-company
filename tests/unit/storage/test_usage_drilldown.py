"""下钻枚举与未归因取样的单测 —— `/usage` 页 ②③ 的取数层（归因 v1 §5.2）。

这个文件盯的是**呈现层最容易把数字弄拧的三个地方**，每一条都对应一种"看着完全
合理、跑起来悄悄给错答案"的写法：

1. **候选清单不许带用量。** 一张能排序的列表一旦带上 token，就会被当成排行榜读，
   而排行榜里没有分母（§2.5）。这条在类型上拦不住（返回的是 dict），只能靠测试。
2. **join 出来的行数不是派工数。** 一个 agent 可能对应多行 workflow_agents / 多条
   task 边，按 join 行数计会把分母吹大、覆盖率随之被系统性压低。
3. **列表行的分母必须等于点开后那张卡的分母。** 两处各写一份 SQL 就会在某次改动
   之后悄悄分叉，而且没有任何东西会报错。
"""

from __future__ import annotations

import pytest

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import AgentModel, KnowledgeLinkModel, WorkflowAgentModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_TO_KIND,
    AGENT_TASK_LINK_TYPE,
    TOKEN_LAYERS,
    AttributionScope,
    DispatchPopulation,
    TokenMetric,
)

DB = "sqlite+aiosqlite://"


@pytest.fixture
async def repo():
    r = StorageRepository(db_url=DB)
    await r.init_db()
    yield r
    await close_db()


async def _agent(
    *,
    agent_id: str,
    role: str = "worker",
    name: str = "w",
    project_id: str | None = "p1",
    session_id: str | None = "s1",
    transcript_path: str | None = None,
    measured: bool = False,
) -> None:
    async with get_session(DB) as session:
        session.add(
            AgentModel(
                id=agent_id,
                team_id="t1",
                name=name,
                role=role,
                created_at=utc_now(),
                session_id=session_id,
                project_id=project_id,
                transcript_path=transcript_path,
                input_tokens=1 if measured else None,
                output_tokens=2 if measured else None,
                cache_creation_tokens=3 if measured else None,
                cache_read_tokens=4 if measured else None,
                tokens_measured_at=utc_now() if measured else None,
            )
        )


async def _wf_agent(*, row_id: str, agent_id: str, run_id: str, cc_agent_id: str) -> None:
    async with get_session(DB) as session:
        session.add(
            WorkflowAgentModel(
                id=row_id,
                run_id=run_id,
                wf_id=run_id,
                cc_agent_id=cc_agent_id,
                os_agent_id=agent_id,
                label="x",
                state="done",
                tokens=0,
                tool_calls=0,
                created_at=utc_now(),
            )
        )


async def _worked_on(agent_id: str, task_id: str) -> None:
    async with get_session(DB) as session:
        session.add(
            KnowledgeLinkModel(
                from_kind=AGENT_TASK_LINK_FROM_KIND,
                from_id=agent_id,
                to_kind=AGENT_TASK_LINK_TO_KIND,
                to_id=task_id,
                link_type=AGENT_TASK_LINK_TYPE,
                context="",
                link_source="test",
                project_id="",
                created_at=utc_now(),
            )
        )


class TestNoUsageInCandidateList:
    """候选清单里出现任何一层 token 就是红线 —— 那是一张没有分母的排行榜。"""

    @pytest.mark.asyncio
    async def test_candidate_rows_carry_counts_only(self, repo):
        await _agent(agent_id="a1", measured=True)
        rows = await repo.list_attribution_scopes(scope=AttributionScope.PROJECT)

        assert rows, "至少要枚举出一个项目"
        for row in rows:
            for layer in TOKEN_LAYERS:
                assert layer not in row, f"候选清单带上了 {layer} —— 用量必须逐 scope 单独查"
            # 允许出现的键就这五个，多一个都要先想清楚它是不是在冒充分母
            assert set(row) == {
                "scope",
                "scope_id",
                "label",
                "dispatches_total",
                "dispatches_attributed",
            }


class TestJoinDoesNotInflateDenominator:
    """多行 join 不得把一个 agent 数成好几次派工。"""

    @pytest.mark.asyncio
    async def test_agent_in_two_workflow_rows_counted_once_per_run(self, repo):
        # 同一个 agent 在同一个 run 里留了两行 workflow_agents（重放/重连都会这样）
        await _agent(agent_id="a1", measured=True)
        await _wf_agent(row_id="r1", agent_id="a1", run_id="wf_1", cc_agent_id="cc1")
        await _wf_agent(row_id="r2", agent_id="a1", run_id="wf_1", cc_agent_id="cc1-retry")

        rows = await repo.list_attribution_scopes(scope=AttributionScope.WORKFLOW_RUN)
        assert len(rows) == 1
        assert rows[0]["dispatches_total"] == 1, "按 join 行数计会数成 2"
        assert rows[0]["dispatches_attributed"] == 1

    @pytest.mark.asyncio
    async def test_agent_on_two_tasks_counted_once_per_task(self, repo):
        await _agent(agent_id="a1", measured=True)
        # 同一条边留两次账在 DB 层就被 knowledge_links 的唯一约束挡住了，
        # 所以这里测的是另一半：一个 agent 横跨两个 task。
        await _worked_on("a1", "task-A")
        await _worked_on("a1", "task-B")

        rows = {r["scope_id"]: r for r in await repo.list_attribution_scopes(scope=AttributionScope.TASK)}
        assert set(rows) == {"task-A", "task-B"}
        assert rows["task-A"]["dispatches_total"] == 1
        # 跨两个 task 留过账 → 四层数切不开，如实计未归因（§2.4，禁止平均分摊）
        assert rows["task-A"]["dispatches_attributed"] == 0
        assert rows["task-B"]["dispatches_attributed"] == 0


class TestListMatchesCard:
    """列表行的分母 == 点开后那张归因卡的分母。分叉了不会报错，只会静静地不一致。"""

    @pytest.mark.asyncio
    async def test_session_row_denominator_equals_attribution_denominator(self, repo):
        await _agent(agent_id="a1", session_id="s1", measured=True)
        await _agent(agent_id="a2", session_id="s1")
        await _agent(agent_id="a3", session_id="s2", measured=True)

        rows = {r["scope_id"]: r for r in await repo.list_attribution_scopes(scope=AttributionScope.SESSION)}
        for scope_id, row in rows.items():
            card = await repo.aggregate_token_attribution(
                metric=TokenMetric.USAGE_SUM,
                scope=AttributionScope.SESSION,
                scope_id=scope_id,
            )
            assert row["dispatches_total"] == card.dispatches_total
            assert row["dispatches_attributed"] == card.dispatches_attributed

    @pytest.mark.asyncio
    async def test_parent_filter_reuses_the_same_scope_filter(self, repo):
        await _agent(agent_id="a1", project_id="p1", session_id="s1")
        await _agent(agent_id="a2", project_id="p2", session_id="s2")

        rows = await repo.list_attribution_scopes(
            scope=AttributionScope.SESSION,
            parent_scope=AttributionScope.PROJECT,
            parent_id="p1",
        )
        assert [r["scope_id"] for r in rows] == ["s1"]


class TestScopeGuards:
    @pytest.mark.asyncio
    async def test_task_scope_rejects_parent_id(self, repo):
        # task 是旁支不是第五级：硬套父级过滤会给出一个看着合理、实则无法解释的子集
        with pytest.raises(ValueError, match="旁支"):
            await repo.list_attribution_scopes(
                scope=AttributionScope.TASK,
                parent_scope=AttributionScope.PROJECT,
                parent_id="p1",
            )

    @pytest.mark.asyncio
    async def test_null_scope_keys_are_dropped_not_grouped(self, repo):
        # session_id 为 NULL 的行不该聚成一个叫"null"的候选 —— 那是个不存在的会话
        await _agent(agent_id="a1", session_id=None)
        await _agent(agent_id="a2", session_id="s1")
        rows = await repo.list_attribution_scopes(scope=AttributionScope.SESSION)
        assert [r["scope_id"] for r in rows] == ["s1"]

    @pytest.mark.asyncio
    async def test_leader_population_is_separate_from_subagents(self, repo):
        await _agent(agent_id="a1", role="worker", session_id="s1")
        await _agent(agent_id="lead", role="leader", session_id="s9")

        sub = await repo.list_attribution_scopes(
            scope=AttributionScope.SESSION, population=DispatchPopulation.SUBAGENT
        )
        leader = await repo.list_attribution_scopes(
            scope=AttributionScope.SESSION, population=DispatchPopulation.LEADER_SESSION
        )
        assert [r["scope_id"] for r in sub] == ["s1"]
        assert [r["scope_id"] for r in leader] == ["s9"]


class TestUnattributedSamples:
    """样例是样例，不是分母 —— 响应必须说清自己只扫了多少行。"""

    @pytest.mark.asyncio
    async def test_samples_are_bounded_and_declare_scan_size(self, repo):
        for i in range(12):
            await _agent(agent_id=f"a{i}", transcript_path=None)

        data = await repo.sample_unattributed(scan_limit=5, per_reason=2)
        assert data["scanned"] == 5, "扫描量必须被 scan_limit 兜住"
        assert data["scan_limit"] == 5
        assert len(data["samples"]["no_transcript_path"]) == 2

    @pytest.mark.asyncio
    async def test_measured_rows_never_appear_as_samples(self, repo):
        await _agent(agent_id="ok", transcript_path="/x.jsonl", measured=True)
        await _agent(agent_id="bad", transcript_path=None)

        data = await repo.sample_unattributed()
        ids = {row["agent_id"] for rows in data["samples"].values() for row in rows}
        assert ids == {"bad"}

    @pytest.mark.asyncio
    async def test_transcript_gone_is_told_apart_from_never_recorded(self, repo):
        # "救不回"与"还没去救"必须分开标 —— 合并会让这张表失去全部价值（§3.4）
        await _agent(agent_id="never", transcript_path=None)
        await _agent(agent_id="gone", transcript_path="/nowhere/does-not-exist.jsonl")

        data = await repo.sample_unattributed()
        assert [r["agent_id"] for r in data["samples"]["no_transcript_path"]] == ["never"]
        assert [r["agent_id"] for r in data["samples"]["transcript_gone"]] == ["gone"]
