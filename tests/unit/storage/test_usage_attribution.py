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
    HarnessId,
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
    harness: str | None = None,
    status: str = "offline",
    last_active_at=None,
    exclusion: str | None = None,
    exclusion_of: str | None = None,
    unmeasurable: str | None = None,
) -> None:
    """直插 AgentModel —— create_agent 不收 created_at / token 五列，而本文件测的
    正好就是这两样东西怎么参与分母与窗口。默认是一行早已结束的派工（offline）。"""
    async with get_session(DB) as session:
        session.add(
            AgentModel(
                id=agent_id,
                team_id=team_id,
                name=name,
                role=role,
                status=status,
                last_active_at=last_active_at,
                attribution_exclusion=exclusion,
                attribution_exclusion_of=exclusion_of,
                usage_unmeasurable=unmeasurable,
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
                harness=harness,
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
        shared = {edge: h for edge, h in hops.items() if edge != "agent->workflow"}
        assert all(h.required == 2 for h in shared.values())
        assert hops["agent->session"].resolvable == 1
        assert hops["agent->task"].resolvable == 1
        # 两行都是直派：没有一行需要 workflow 这条边
        assert (hops["agent->workflow"].required, hops["agent->workflow"].resolvable) == (0, 0)

    @pytest.mark.asyncio
    async def test_workflow_hop_counts_only_workflow_dispatches(self, repo):
        """agent->workflow 的分母只数 workflow 派工（设计 §4.1，缔造者 09-30 同意缩小）。

        09-30 生产库：分母用全部子 agent 行时显示 74.0%，其中 1,364 行直派本就不属于
        任何 workflow run；按设计只算 workflow 派工是 96.0%。三种形态都要认出来：
        role 是 workflow-subagent、路径在 subagents/workflows/ 下、或 workflow_agents 里有它。
        """
        await _agent(repo, agent_id="direct")
        await _agent(repo, agent_id="wf-linked", role="workflow-subagent")
        await _agent(repo, agent_id="wf-unlinked", role="workflow-subagent")
        await _agent(
            repo, agent_id="by-path", role="general-purpose",
            transcript_path="/p/s/subagents/workflows/wf_1/agent-x.jsonl",
        )
        async with get_session(DB) as session:
            session.add(WorkflowAgentModel(
                id="wa1", run_id="wf_1", wf_id="wf_1", cc_agent_id="c1", os_agent_id="wf-linked",
                label="x", state="done", tokens=0, tool_calls=0, created_at=utc_now(),
            ))

        report = await repo.usage_coverage_report()
        hop = next(h for h in report.hops if h.edge == "agent->workflow")
        assert (hop.resolvable, hop.required) == (1, 3)
        session_hop = next(h for h in report.hops if h.edge == "agent->session")
        assert session_hop.required == 4, "其它各跳的分母不变，仍是全部子 agent 行"

    @pytest.mark.asyncio
    async def test_hops_are_split_by_harness(self, repo):
        """Codex 行不进 CC 的分母：各跳与矩阵一样按 harness 各算各的。"""
        await _agent(repo, agent_id="cc", session_id="s1", harness=HarnessId.CLAUDE_CODE.value)
        await _agent(repo, agent_id="cx1", harness=HarnessId.CODEX.value)
        await _agent(repo, agent_id="cx2", harness=HarnessId.CODEX.value)

        report = await repo.usage_coverage_report()
        session_hops = {h.harness: h for h in report.hops if h.edge == "agent->session"}
        assert set(session_hops) == {HarnessId.CLAUDE_CODE, HarnessId.CODEX}
        assert (session_hops[HarnessId.CLAUDE_CODE].resolvable,
                session_hops[HarnessId.CLAUDE_CODE].required) == (1, 1)
        assert (session_hops[HarnessId.CODEX].resolvable,
                session_hops[HarnessId.CODEX].required) == (0, 2)


class TestHarnessBuckets:
    """覆盖率按 harness 分列 —— 两个 harness 的分子分母**禁止相加**（r5 §6.6）。

    这是分母被悄悄做假的又一种形态，而且是最不像做假的那一种：两个 harness 的行
    本来就都在 ``agents`` 表里，一句 ``GROUP BY`` 都不写就自然合成了一个数。合出来
    的覆盖率既不是 CC 的也不是 Codex 的，没有任何办法被证伪 —— 它只是"看起来还行"。
    """

    @pytest.mark.asyncio
    async def test_unlabelled_only_db_keeps_the_original_matrix_shape(self, repo):
        """全库都没标注 harness 时，矩阵形状与 harness 维度引入之前完全一致。

        这条钉的是"引入维度没有动 CC"：一个 harness 都没标过的库（也就是今天的每
        一个既有库）必须恰好拿到四行，且 harness 一律为 None —— 不是 claude-code。
        """
        await _agent(repo, agent_id="a1", measured=True)
        await _agent(repo, agent_id="L1", role="leader", name="Leader")

        report = await repo.usage_coverage_report()
        assert [r.path for r in report.rows] == [
            DispatchPopulation.SUBAGENT,
            DispatchPopulation.LEADER_SESSION,
            DispatchPopulation.WORKFLOW_SELF_REPORT,
            DispatchPopulation.TOOL_CALL,
        ]
        assert all(r.harness is None for r in report.rows)

    @pytest.mark.asyncio
    async def test_two_harnesses_get_two_rows_that_are_never_summed(self, repo):
        """一 CC 一 Codex：子 agent 路径拆成两行，各带各的分母。"""
        await _agent(repo, agent_id="cc1", harness=HarnessId.CLAUDE_CODE.value, measured=True)
        await _agent(repo, agent_id="cc2", harness=HarnessId.CLAUDE_CODE.value)
        await _agent(repo, agent_id="cx1", harness=HarnessId.CODEX.value, measured=True)

        report = await repo.usage_coverage_report()
        sub = [r for r in report.rows if r.path is DispatchPopulation.SUBAGENT]
        by_harness = {r.harness: r for r in sub}
        assert set(by_harness) == {HarnessId.CLAUDE_CODE, HarnessId.CODEX}
        assert by_harness[HarnessId.CLAUDE_CODE].dispatches_total == 2
        assert by_harness[HarnessId.CLAUDE_CODE].dispatches_attributed == 1
        assert by_harness[HarnessId.CODEX].dispatches_total == 1
        assert by_harness[HarnessId.CODEX].dispatches_attributed == 1
        # 分列的意义就在这里：没有任何一行的分母是 3。
        assert all(r.dispatches_total != 3 for r in sub)

    @pytest.mark.asyncio
    async def test_unlabelled_rows_get_their_own_bucket_not_folded_into_cc(self, repo):
        """未标注的行自成一桶 —— 并进 claude-code 就是给历史行编造一个判断。"""
        await _agent(repo, agent_id="cc1", harness=HarnessId.CLAUDE_CODE.value)
        await _agent(repo, agent_id="old1")  # harness IS NULL

        sub = [
            r
            for r in (await repo.usage_coverage_report()).rows
            if r.path is DispatchPopulation.SUBAGENT
        ]
        by_harness = {r.harness: r for r in sub}
        assert set(by_harness) == {HarnessId.CLAUDE_CODE, None}
        assert by_harness[HarnessId.CLAUDE_CODE].dispatches_total == 1
        assert by_harness[None].dispatches_total == 1

    @pytest.mark.asyncio
    async def test_empty_path_still_yields_one_row(self, repo):
        """空库也必须有行：分母为 0 与"这条路径不存在"是两回事。

        后者会让一整条路径从页面上无声消失，而消失的东西没有人会去问它为什么是 0。
        """
        rows = (await repo.usage_coverage_report()).rows
        sub = [r for r in rows if r.path is DispatchPopulation.SUBAGENT]
        assert len(sub) == 1
        assert sub[0].dispatches_total == 0
        assert sub[0].harness is None

    @pytest.mark.asyncio
    async def test_default_aggregate_call_is_unchanged_by_the_harness_param(self, repo):
        """不传 harness 时，聚合结果与 harness 维度引入之前逐字段相同（CC 零漂移）。"""
        await _agent(repo, agent_id="cc1", harness=HarnessId.CLAUDE_CODE.value, measured=True)
        await _agent(repo, agent_id="cx1", harness=HarnessId.CODEX.value, measured=True)
        await _agent(repo, agent_id="old1", measured=True)

        whole = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert whole.dispatches_total == 3  # ANY_HARNESS = 不收窄，三行都在
        buckets = [
            await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM, harness=h)
            for h in (HarnessId.CLAUDE_CODE.value, HarnessId.CODEX.value, None)
        ]
        assert [b.dispatches_total for b in buckets] == [1, 1, 1]


class TestExcludedRows:
    """打了排除标记的行不计入任何统计，排除了多少同屏报出（缔造者 09-30 裁定）。

    生产形态：同一份 transcript 两行都有账（幽灵行上是 08-03 回采的中途快照），
    Leader output 多算 19%；resume 空壳 Leader 行没有 transcript、零工具调用，却在
    分母里把近 14 天的覆盖率压成 6/31。
    """

    async def _seed(self, repo):
        await _agent(repo, agent_id="real", role="leader", name="Leader", session_id="s1",
                     transcript_path="/p/s1.jsonl", measured=True, layers=(1, 100, 3, 4))
        await _agent(repo, agent_id="ghost", role="leader", name="Leader",
                     transcript_path="/p/s1.jsonl", measured=True, layers=(1, 40, 3, 4),
                     exclusion="duplicate_transcript", exclusion_of="real")
        await _agent(repo, agent_id="shell", role="leader", name="Leader", session_id="s2",
                     exclusion="resume_shell")
        await _agent(repo, agent_id="sub", project_id="p1", session_id="s1")
        await _agent(repo, agent_id="sub-excluded", project_id="p1", session_id="s1",
                     exclusion="duplicate_transcript", exclusion_of="sub")

    @pytest.mark.asyncio
    async def test_excluded_rows_leave_every_statistic(self, repo):
        await self._seed(repo)

        leader = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, population=DispatchPopulation.LEADER_SESSION
        )
        assert (leader.dispatches_attributed, leader.dispatches_total) == (1, 1)
        assert leader.output_tokens == 100, "重复行的中途快照又被加了一遍"
        assert leader.unattributed_reasons == {}, "空壳留在了分母里"

        report = await repo.usage_coverage_report()
        rows = {r.path: r for r in report.rows}
        assert rows[DispatchPopulation.LEADER_SESSION].dispatches_total == 1
        assert rows[DispatchPopulation.SUBAGENT].dispatches_total == 1
        assert all(h.required == 1 for h in report.hops if h.edge != "agent->workflow")
        assert report.excluded_rows == {"duplicate_transcript": 2, "resume_shell": 1}

        samples = await repo.sample_unattributed(population=DispatchPopulation.LEADER_SESSION)
        assert samples["samples"] == {}

        scopes = await repo.list_attribution_scopes(
            scope=AttributionScope.PROJECT, population=DispatchPopulation.SUBAGENT
        )
        assert [(s["scope_id"], s["dispatches_total"]) for s in scopes] == [("p1", 1)]

    @pytest.mark.asyncio
    async def test_marks_survive_a_round_trip_and_unrelated_writes(self, repo):
        """标记在列上，不在 config：别的写方整体替换 config 不会把它抹掉。"""
        await self._seed(repo)
        await repo.update_agent("ghost", config={"note": "unrelated writer"}, status="offline")

        row = await repo.get_agent("ghost")
        assert row.attribution_exclusion == "duplicate_transcript"
        assert row.attribution_exclusion_of == "real"
        leader = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, population=DispatchPopulation.LEADER_SESSION
        )
        assert leader.output_tokens == 100


class TestReasonsBeyondTheOriginalThree:
    """未归因分类的五个新码（报告 c20b37a8 W3）：原先这些行全被标成"跑一次回采就能补上"。"""

    @pytest.mark.asyncio
    async def test_codex_rows_are_unsupported_or_archived_never_recoverable(
        self, repo, tmp_path, monkeypatch
    ):
        from aiteam.services import usage_coverage

        home = tmp_path / "codex"
        (home / "archived_sessions").mkdir(parents=True)
        (home / "archived_sessions" / "rollout-a.jsonl").write_text("{}\n")
        live = tmp_path / "rollout-b.jsonl"
        live.write_text("{}\n")
        monkeypatch.setattr(usage_coverage, "codex_home", lambda: home)

        codex = HarnessId.CODEX.value
        await _agent(repo, agent_id="live", harness=codex, transcript_path=str(live))
        await _agent(repo, agent_id="archived", harness=codex,
                     transcript_path=str(tmp_path / "sessions" / "rollout-a.jsonl"))
        await _agent(repo, agent_id="gone", harness=codex,
                     transcript_path=str(tmp_path / "sessions" / "rollout-z.jsonl"))
        await _agent(repo, agent_id="nopath", harness=codex)

        att = await repo.aggregate_token_attribution(
            metric=TokenMetric.USAGE_SUM, harness=codex
        )
        assert att.unattributed_reasons == {
            UnattributedReason.HARNESS_UNSUPPORTED.value: 2,
            UnattributedReason.TRANSCRIPT_ARCHIVED.value: 1,
            UnattributedReason.TRANSCRIPT_GONE.value: 1,
        }

    @pytest.mark.asyncio
    async def test_running_and_error_only_rows_are_told_apart(self, repo, tmp_path):
        alive = tmp_path / "agent-a.jsonl"
        alive.write_text("{}\n")
        now = utc_now()
        await _agent(repo, agent_id="busy", status="busy")
        await _agent(repo, agent_id="recent", transcript_path=str(alive),
                     last_active_at=now - timedelta(minutes=5))
        await _agent(repo, agent_id="api-error", transcript_path=str(alive),
                     unmeasurable=UnattributedReason.API_ERROR_ONLY.value)
        await _agent(repo, agent_id="stale", transcript_path=str(alive),
                     last_active_at=now - timedelta(hours=2))
        await _agent(repo, agent_id="never")

        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.unattributed_reasons == {
            UnattributedReason.IN_FLIGHT.value: 2,
            UnattributedReason.API_ERROR_ONLY.value: 1,
            UnattributedReason.NOT_YET_MEASURED.value: 1,
            UnattributedReason.NO_TRANSCRIPT_PATH.value: 1,
        }
        samples = await repo.sample_unattributed()
        assert {r["agent_id"] for r in samples["samples"]["in_flight"]} == {"busy", "recent"}

    @pytest.mark.asyncio
    async def test_live_evidence_outranks_a_frozen_verdict(self, repo, tmp_path):
        """行上写着"首个回复前就结束"，但行又 busy 了：它还在长，显示在飞，不显示旧结论。"""
        alive = tmp_path / "agent-b.jsonl"
        alive.write_text("{}\n")
        await _agent(repo, agent_id="resumed", transcript_path=str(alive), status="busy",
                     unmeasurable=UnattributedReason.NO_USAGE_LINES.value)

        att = await repo.aggregate_token_attribution(metric=TokenMetric.USAGE_SUM)
        assert att.unattributed_reasons == {UnattributedReason.IN_FLIGHT.value: 1}
