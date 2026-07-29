"""覆盖率聚合单测 —— 分母定义与未归因分类（归因 v1 §4.1 / §3.4 / §2.4）。

**这个文件存在的理由是分母**。§4.1 原文：「分母的定义必须写死在代码里并有单测，
因为分母是唯一能被悄悄做假的地方」。R2 说的做假不是恶意，是一个看起来完全合理的
改动——"没路径的行没法测，从分母里去掉吧"——覆盖率于是从 78% 跳到 100%，数字变好
看、代码读着也通顺、没有任何东西报错。下面每一条断言都对应一种这样的改法。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aiteam.clock import utc_now
from aiteam.services.usage_coverage import classify_unattributed
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import AgentModel, KnowledgeLinkModel, WorkflowAgentModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_TO_KIND,
    AGENT_TASK_LINK_TYPE,
    AttributionMethod,
    AttributionScope,
    DispatchPopulation,
    TokenMetric,
    TokenSource,
    UnattributedReason,
)

DB = "sqlite+aiosqlite://"


@pytest.fixture
async def repo():
    r = StorageRepository(db_url=DB)
    await r.init_db()
    yield r
    await close_db()


async def _agent(
    repo: StorageRepository,
    *,
    agent_id: str,
    role: str = "worker",
    name: str = "w",
    team_id: str = "t1",
    created_at=None,
    session_id: str | None = None,
    project_id: str | None = None,
    transcript_path: str | None = None,
    measured: bool = False,
    layers: tuple[int, int, int, int] = (1, 2, 3, 4),
    tokens_source: str | None = None,
) -> None:
    """直插 AgentModel —— create_agent 不收 created_at / token 五列，而本文件测的
    正好就是这两样东西怎么参与分母与窗口。"""
    async with get_session(DB) as session:
        session.add(
            AgentModel(
                id=agent_id,
                team_id=team_id,
                name=name,
                role=role,
                created_at=created_at or utc_now(),
                session_id=session_id,
                project_id=project_id,
                transcript_path=transcript_path,
                input_tokens=layers[0] if measured else None,
                output_tokens=layers[1] if measured else None,
                cache_creation_tokens=layers[2] if measured else None,
                cache_read_tokens=layers[3] if measured else None,
                tokens_measured_at=utc_now() if measured else None,
                tokens_source=tokens_source,
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


class TestDenominator:
    """分母 = ``role != 'leader'`` 的行数，**含**那些没有 transcript 的行。"""

    @pytest.mark.asyncio
    async def test_rows_without_transcript_stay_in_denominator(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x.jsonl", measured=True)
        await _agent(repo, agent_id="a2", transcript_path=None)
        await _agent(repo, agent_id="a3", transcript_path=None)

        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        # 3 而不是 1：不得以"没路径所以不算"为由剔除——那正是让局部冒充全貌
        assert att.dispatches_total == 3
        assert att.dispatches_attributed == 1
        assert att.unattributed_reasons == {UnattributedReason.NO_TRANSCRIPT_PATH.value: 2}

    @pytest.mark.asyncio
    async def test_leader_rows_are_a_separate_population(self, repo):
        await _agent(repo, agent_id="a1", role="worker")
        await _agent(repo, agent_id="L1", role="leader", name="Leader")
        await _agent(repo, agent_id="L2", role="leader", name="Leader")

        sub = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        leader = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, population=DispatchPopulation.LEADER_SESSION
        )
        assert sub.dispatches_total == 1
        assert leader.dispatches_total == 2

    @pytest.mark.asyncio
    async def test_workflow_subagents_are_not_excluded(self, repo):
        """实测 96% 的派工是 ``workflow-subagent``；把它们移出分母覆盖率立刻虚高。

        ``api/agent_reuse.py`` 确实有个 ``_EXCLUDED_ROLES`` 把它连同 leader 一起
        排除，但那是"谁能被复用"的治理口径。在归因口径里 workflow 派工就是派工。
        """
        await _agent(repo, agent_id="a1", role="workflow-subagent")
        await _agent(repo, agent_id="a2", role="workflow-subagent")
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.dispatches_total == 2

    @pytest.mark.asyncio
    async def test_numerator_plus_unattributed_equals_denominator(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x.jsonl", measured=True)
        await _agent(repo, agent_id="a2", transcript_path=None)
        await _agent(repo, agent_id="a3", transcript_path="/gone.jsonl")
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.dispatches_attributed + sum(att.unattributed_reasons.values()) == (
            att.dispatches_total
        )


class TestWindowIsOnCreatedAt:
    """窗口落在 ``created_at``，**不是** ``tokens_measured_at``。

    按测量时间落窗，没测过的行会连同分母一起消失，覆盖率恒等于 100% —— 而且看起来
    完全说得通（"只统计这段时间测到的"）。这是分母做假里最隐蔽的一种。
    """

    @pytest.mark.asyncio
    async def test_old_unmeasured_row_leaves_the_window_with_its_denominator(self, repo):
        now = utc_now()
        await _agent(repo, agent_id="old", created_at=now - timedelta(days=30))
        await _agent(repo, agent_id="new", created_at=now - timedelta(days=1))

        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, since=now - timedelta(days=7), until=now
        )
        assert att.dispatches_total == 1

    @pytest.mark.asyncio
    async def test_measured_today_but_created_long_ago_is_out_of_a_recent_window(self, repo):
        """回采会给三周前的行盖上今天的 ``tokens_measured_at``。

        若按测量时间落窗，"近 7 天"会突然多出 1,900 行历史派工，且它们全部已测量
        —— 近期采集率会被回采洗成接近 100%（R 6.3-2 点名的"一个数字掩盖另一个数字"）。
        """
        now = utc_now()
        await _agent(
            repo,
            agent_id="backfilled",
            created_at=now - timedelta(days=21),
            transcript_path="/x.jsonl",
            measured=True,  # tokens_measured_at = 现在
        )
        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, since=now - timedelta(days=7), until=now
        )
        assert att.dispatches_total == 0
        assert att.dispatches_attributed == 0


class TestUnattributedReasons:
    @pytest.mark.asyncio
    async def test_three_reasons_are_told_apart(self, repo, tmp_path):
        alive = tmp_path / "alive.jsonl"
        alive.write_text("{}", encoding="utf-8")

        await _agent(repo, agent_id="a1", transcript_path=None)
        await _agent(repo, agent_id="a2", transcript_path=str(tmp_path / "vanished.jsonl"))
        await _agent(repo, agent_id="a3", transcript_path=str(alive))

        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.unattributed_reasons == {
            UnattributedReason.NO_TRANSCRIPT_PATH.value: 1,
            UnattributedReason.TRANSCRIPT_GONE.value: 1,
            UnattributedReason.NOT_YET_MEASURED.value: 1,
        }

    def test_classifier_separates_recoverable_from_lost(self, tmp_path):
        """``not_yet_measured`` 跑一次回采就能补；另外两个补不上。合并两者，
        看的人就无从判断该不该现在动手（回采窗口在关闭，R6）。"""
        assert classify_unattributed(None) == UnattributedReason.NO_TRANSCRIPT_PATH.value
        assert classify_unattributed("") == UnattributedReason.NO_TRANSCRIPT_PATH.value
        assert (
            classify_unattributed("/nope.jsonl", file_probe=lambda _p: False)
            == UnattributedReason.TRANSCRIPT_GONE.value
        )
        assert (
            classify_unattributed("/yes.jsonl", file_probe=lambda _p: True)
            == UnattributedReason.NOT_YET_MEASURED.value
        )


class TestScopes:
    @pytest.mark.asyncio
    async def test_scope_id_filters_and_empty_means_whole_ledger(self, repo):
        await _agent(repo, agent_id="a1", project_id="p1", transcript_path="/x", measured=True)
        await _agent(repo, agent_id="a2", project_id="p2", transcript_path="/x", measured=True)

        whole = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        scoped = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.PROJECT, scope_id="p1"
        )
        assert whole.dispatches_total == 2
        assert scoped.dispatches_total == 1
        assert scoped.scope_id == "p1"

    @pytest.mark.asyncio
    async def test_layers_are_summed_only_over_attributed_rows(self, repo):
        await _agent(
            repo, agent_id="a1", transcript_path="/x", measured=True, layers=(1, 2, 3, 4)
        )
        await _agent(
            repo, agent_id="a2", transcript_path="/x", measured=True, layers=(10, 20, 30, 40)
        )
        await _agent(repo, agent_id="a3", transcript_path=None)
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert (att.input_tokens, att.output_tokens) == (11, 22)
        assert (att.cache_creation_tokens, att.cache_read_tokens) == (33, 44)

    @pytest.mark.asyncio
    async def test_alias_fallback_downgrades_the_whole_answer(self, repo):
        await _agent(
            repo,
            agent_id="a1",
            transcript_path="/x",
            measured=True,
            tokens_source=TokenSource.TRANSCRIPT.value,
        )
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.method is AttributionMethod.TRANSCRIPT_PARSE

        await _agent(
            repo,
            agent_id="a2",
            transcript_path="/y",
            measured=True,
            tokens_source=TokenSource.ALIAS_FALLBACK.value,
        )
        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        # 一行兜底就整体降级：口径诚实优先于好看
        assert att.method is AttributionMethod.ALIAS_FALLBACK

    @pytest.mark.asyncio
    async def test_ctx_last_is_refused_with_the_reason(self, repo):
        """ctx_last 在结构上进不来：四层分解在 ingest 时就已合并、从未落库。

        把知识放在拒绝路径上，比放在注释里更难被绕过。
        """
        with pytest.raises(ValueError, match="四层"):
            await repo.aggregate_token_attribution(metric=TokenMetric.CTX_LAST)


class TestTaskScope:
    """§2.4：跨多个 task 的 agent 如实计未归因，**禁止平均分摊**。"""

    @pytest.mark.asyncio
    async def test_single_edge_agent_attributes_cleanly(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x", measured=True, layers=(1, 2, 3, 4))
        await _worked_on("a1", "task-A")
        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id="task-A"
        )
        assert (att.dispatches_attributed, att.dispatches_total) == (1, 1)
        assert att.output_tokens == 2

    @pytest.mark.asyncio
    async def test_multi_task_agent_is_unattributed_not_split(self, repo):
        await _agent(
            repo, agent_id="a1", transcript_path="/x", measured=True, layers=(10, 20, 30, 40)
        )
        await _worked_on("a1", "task-A")
        await _worked_on("a1", "task-B")

        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id="task-A"
        )
        assert att.dispatches_total == 1
        assert att.dispatches_attributed == 0
        assert att.unattributed_reasons == {
            UnattributedReason.MULTI_TASK_UNSPLITTABLE.value: 1
        }
        # 关键：一个 token 都不许分过来。平均分摊会造出无法证伪的数字。
        assert (att.input_tokens, att.output_tokens) == (0, 0)
        assert (att.cache_creation_tokens, att.cache_read_tokens) == (0, 0)

    @pytest.mark.asyncio
    async def test_empty_task_id_is_refused_instead_of_mislabelling(self, repo):
        """空 task_id 曾让"根本没有边"的行被标成"切不开"。

        两者处置完全相反：前者要靠记账动作把边攒起来（全链最窄的一跳，0.8%），
        后者是边有了但数分不开。混成一个数，看的人就再也看不见真正的瓶颈。
        """
        await _agent(repo, agent_id="a1")
        with pytest.raises(ValueError, match="scope_id"):
            await repo.aggregate_token_attribution(
                metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id=""
            )

    @pytest.mark.asyncio
    async def test_task_with_no_edges_is_zero_over_zero(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x", measured=True)
        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, scope=AttributionScope.TASK, scope_id="nobody"
        )
        assert (att.dispatches_attributed, att.dispatches_total) == (0, 0)


class TestCoverageReport:
    @pytest.mark.asyncio
    async def test_matrix_has_all_four_paths_and_no_total_row(self, repo):
        await _agent(repo, agent_id="a1", transcript_path="/x", measured=True)
        await _agent(repo, agent_id="L1", role="leader", name="Leader")
        async with get_session(DB) as session:
            session.add(
                WorkflowAgentModel(
                    id="wa1", run_id="wf_1", wf_id="wf_1", cc_agent_id="c1", tokens=100
                )
            )
            session.add(
                WorkflowAgentModel(
                    id="wa2", run_id="wf_1", wf_id="wf_1", cc_agent_id="c2", tokens=0
                )
            )

        report = await repo.usage_coverage_report()
        paths = [r.path for r in report.rows]
        assert paths == [
            DispatchPopulation.SUBAGENT,
            DispatchPopulation.LEADER_SESSION,
            DispatchPopulation.WORKFLOW_SELF_REPORT,
            DispatchPopulation.TOOL_CALL,
        ]
        by_path = {r.path: r for r in report.rows}
        assert by_path[DispatchPopulation.WORKFLOW_SELF_REPORT].metric == (
            TokenMetric.CTX_LAST.value
        )
        assert by_path[DispatchPopulation.WORKFLOW_SELF_REPORT].dispatches_attributed == 1
        assert by_path[DispatchPopulation.WORKFLOW_SELF_REPORT].unattributed_reasons == {
            UnattributedReason.SELF_REPORT_ABSENT.value: 1
        }

    @pytest.mark.asyncio
    async def test_not_collected_row_is_none_not_zero(self, repo):
        """"设计上不采集"是一个正式取值。0 会被读成"一次都没测到"，空白会被读成 bug。"""
        report = await repo.usage_coverage_report()
        tool_row = next(r for r in report.rows if r.path is DispatchPopulation.TOOL_CALL)
        assert tool_row.dispatches_total is None
        assert tool_row.dispatches_attributed is None
        assert tool_row.metric == ""

    @pytest.mark.asyncio
    async def test_metrics_are_never_mixed_across_rows(self, repo):
        """矩阵不提供合计行：两个口径实测差 5~25 倍，跨行合计就是混口径。"""
        report = await repo.usage_coverage_report()
        metrics = {r.metric for r in report.rows if r.metric}
        assert metrics == {TokenMetric.USAGE_SUM.value, TokenMetric.CTX_LAST.value}
        assert not hasattr(report, "total")

    @pytest.mark.asyncio
    async def test_hops_cover_the_whole_chain_and_share_one_denominator(self, repo):
        await _agent(repo, agent_id="a1", session_id="s1", project_id="p1", transcript_path="/x")
        await _agent(repo, agent_id="a2")
        await _worked_on("a1", "task-A")

        report = await repo.usage_coverage_report()
        hops = {h.edge: h for h in report.hops}
        assert set(hops) == {
            "agent->session",
            "agent->project",
            "agent->workflow",
            "agent->transcript",
            "agent->task",
        }
        assert all(h.required == 2 for h in hops.values())
        assert hops["agent->session"].resolvable == 1
        assert hops["agent->task"].resolvable == 1
        assert hops["agent->workflow"].resolvable == 0
