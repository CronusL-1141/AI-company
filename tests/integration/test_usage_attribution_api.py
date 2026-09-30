"""用量归因端到端 —— 契约 ② 与三个记账动作的寄生写边（归因 v1 §2.5 / §2.4）。

契约 ② 原文：「任何 API 响应体中，token 数值字段必与 ``dispatches_total``、
``metric`` 同层出现，缺一即测试失败」。这里不按端点逐个手写断言，而是**递归遍历
整个响应体**：任何一个含 token 层字段的 dict，都必须在同一个 dict 里带上分母与
口径。这样将来无论谁把归因结果嵌到哪个新端点的哪一层里，这条都自动生效。
"""

from __future__ import annotations

import pytest

from aiteam.api import deps
from aiteam.api.task_edge import SESSION_HEADER
from aiteam.clock import utc_now
from aiteam.storage.connection import get_session
from aiteam.storage.models import AgentModel
from aiteam.types import (
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_TYPE,
    TOKEN_LAYERS,
)

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


def _assert_tokens_never_travel_alone(node, path="$"):
    """递归：任何带 token 层的 dict 必须同层带 dispatches_total 与 metric。"""
    if isinstance(node, dict):
        if any(layer in node for layer in TOKEN_LAYERS):
            assert "dispatches_total" in node, f"{path}: token 数值脱离分母出现"
            assert "metric" in node, f"{path}: token 数值脱离口径出现"
            assert "total" not in node and "total_tokens" not in node, (
                f"{path}: 出现了裸合计字段"
            )
        for key, value in node.items():
            _assert_tokens_never_travel_alone(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _assert_tokens_never_travel_alone(item, f"{path}[{i}]")


async def _seed_agent(agent_id: str, name: str, *, session_id: str | None = SESSION) -> None:
    async with get_session("sqlite+aiosqlite://") as session:
        session.add(
            AgentModel(
                id=agent_id,
                team_id="t1",
                name=name,
                role="worker",
                session_id=session_id,
                transcript_path="/tmp/nonexistent.jsonl",
                created_at=utc_now(),
            )
        )


class TestSameLayerContract:
    def test_attribution_endpoint_never_returns_a_lonely_number(self, integration_client):
        resp = integration_client.get("/api/usage/attribution")
        assert resp.status_code == 200
        body = resp.json()
        _assert_tokens_never_travel_alone(body)
        data = body["data"]
        assert set(TOKEN_LAYERS) <= set(data)
        assert data["metric"] == "usage_sum"
        assert "dispatches_total" in data and "unattributed_reasons" in data

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "?scope=project&scope_id=p1",
            "?scope=session&scope_id=s1",
            "?scope=agent&scope_id=a1",
            "?scope=workflow_run&scope_id=wf_1",
            "?scope=task&scope_id=some-task",
            "?population=leader_session",
            "?days=7",
        ],
    )
    def test_no_parameter_combination_yields_a_bare_total(self, integration_client, query):
        """§2.5 验收原话：「API 在任何参数组合下都无法返回孤立总量」。"""
        resp = integration_client.get(f"/api/usage/attribution{query}")
        assert resp.status_code == 200
        _assert_tokens_never_travel_alone(resp.json())

    def test_coverage_endpoint_carries_no_token_values_at_all(self, integration_client):
        """覆盖率矩阵是覆盖率表，不是 token 值表 —— 一个 token 数字都不该有。"""
        resp = integration_client.get("/api/usage/coverage")
        assert resp.status_code == 200
        body = resp.json()
        _assert_tokens_never_travel_alone(body)
        for row in body["data"]["rows"]:
            assert not any(layer in row for layer in TOKEN_LAYERS)

    def test_not_collected_row_is_null_not_zero(self, integration_client):
        resp = integration_client.get("/api/usage/coverage")
        rows = {r["path"]: r for r in resp.json()["data"]["rows"]}
        assert rows["tool_call"]["dispatches_total"] is None

    def test_ctx_last_is_rejected_with_its_reason(self, integration_client):
        resp = integration_client.get("/api/usage/attribution?metric=ctx_last")
        assert resp.status_code == 400
        assert "四层" in resp.json()["detail"]

    def test_bad_enum_is_a_400_not_a_silent_default(self, integration_client):
        assert integration_client.get("/api/usage/attribution?scope=nope").status_code == 400
        assert (
            integration_client.get("/api/usage/attribution?population=nope").status_code == 400
        )

    def test_task_scope_without_id_is_refused(self, integration_client):
        """否则"没有边"会被标成"切不开"，全链最窄的那一跳就被藏起来了。"""
        resp = integration_client.get("/api/usage/attribution?scope=task")
        assert resp.status_code == 400
        assert "agent->task" in resp.json()["detail"]


def _run(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro)


async def _edges_for(task_id):
    links = await deps._repository.find_knowledge_links("task", task_id, direction="in")
    return [
        lk
        for lk in links
        if lk.from_kind == AGENT_TASK_LINK_FROM_KIND and lk.link_type == AGENT_TASK_LINK_TYPE
    ]


class TestLedgerActionsWriteTheEdge:
    """§2.4：三个**已经必然发生**的记账动作顺手把 agent→task 边写下来。"""

    def _edges(self, task_id):
        return _edges_for(task_id)

    def _make_task(self, client, title="归因边测试"):
        task = _run(deps._repository.create_task(team_id="t1", title=title))
        return "t1", task.id

    def test_task_memo_add_binds_the_author(self, integration_client):
        import asyncio

        asyncio.get_event_loop().run_until_complete(_seed_agent("a1", "scribe"))
        _team_id, task_id = self._make_task(integration_client)

        resp = integration_client.post(
            f"/api/tasks/{task_id}/memo",
            json={"content": "进展一则", "author": "scribe", "type": "progress"},
            headers={SESSION_HEADER: SESSION},
        )
        assert resp.status_code == 200
        edges = asyncio.get_event_loop().run_until_complete(self._edges(task_id))
        assert [e.from_id for e in edges] == ["a1"]

    def test_memo_without_session_header_writes_no_edge(self, integration_client):
        import asyncio

        asyncio.get_event_loop().run_until_complete(_seed_agent("a1", "scribe"))
        _team_id, task_id = self._make_task(integration_client)

        resp = integration_client.post(
            f"/api/tasks/{task_id}/memo",
            json={"content": "无会话头", "author": "scribe", "type": "progress"},
        )
        # 记账动作本身照常成功 —— 归因是观测，观测拿不到域不该拖垮被观测的动作
        assert resp.status_code == 200
        assert asyncio.get_event_loop().run_until_complete(self._edges(task_id)) == []

    def test_unresolvable_author_writes_no_edge_but_memo_still_lands(self, integration_client):
        import asyncio

        _team_id, task_id = self._make_task(integration_client)
        resp = integration_client.post(
            f"/api/tasks/{task_id}/memo",
            json={"content": "作者解析不到", "author": "d3-p2-typelayer", "type": "progress"},
            headers={SESSION_HEADER: SESSION},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["content"] == "作者解析不到"
        assert asyncio.get_event_loop().run_until_complete(self._edges(task_id)) == []

    def test_task_update_binds_only_when_assigned_to_is_given(self, integration_client):
        import asyncio

        asyncio.get_event_loop().run_until_complete(_seed_agent("a1", "doer"))
        _team_id, task_id = self._make_task(integration_client)

        integration_client.put(
            f"/api/tasks/{task_id}",
            json={"status": "running"},
            headers={SESSION_HEADER: SESSION},
        )
        assert asyncio.get_event_loop().run_until_complete(self._edges(task_id)) == []

        integration_client.put(
            f"/api/tasks/{task_id}",
            json={"assigned_to": "doer"},
            headers={SESSION_HEADER: SESSION},
        )
        edges = asyncio.get_event_loop().run_until_complete(self._edges(task_id))
        assert [e.from_id for e in edges] == ["a1"]

    def test_report_save_binds_when_linked_to_a_task(self, integration_client):
        import asyncio

        asyncio.get_event_loop().run_until_complete(_seed_agent("a1", "researcher"))
        _team_id, task_id = self._make_task(integration_client)

        resp = integration_client.post(
            "/api/reports",
            json={
                "author": "researcher",
                "topic": "归因",
                "content": "正文",
                "report_type": "research",
                "task_id": task_id,
            },
            headers={SESSION_HEADER: SESSION},
        )
        assert resp.status_code == 201
        edges = asyncio.get_event_loop().run_until_complete(self._edges(task_id))
        assert [e.from_id for e in edges] == ["a1"]

    def test_edge_moves_the_task_hop_coverage(self, integration_client):
        """写边之前 agent→task 是 0，之后立刻升 —— 推导器与写侧共用同一份词汇。"""
        import asyncio

        asyncio.get_event_loop().run_until_complete(_seed_agent("a1", "scribe"))
        _team_id, task_id = self._make_task(integration_client)

        before = integration_client.get("/api/usage/coverage").json()["data"]["hops"]
        assert next(h for h in before if h["edge"] == "agent->task")["resolvable"] == 0

        integration_client.post(
            f"/api/tasks/{task_id}/memo",
            json={"content": "留账", "author": "scribe", "type": "progress"},
            headers={SESSION_HEADER: SESSION},
        )
        after = integration_client.get("/api/usage/coverage").json()["data"]["hops"]
        assert next(h for h in after if h["edge"] == "agent->task")["resolvable"] == 1


class TestUnattributedHarnessParam:
    """/unattributed 的 harness 参数：不过滤、未标注、具体 harness 三种取值必须分得开。

    4d87eba2：抽屉点的是矩阵里的 (path, harness) 一行，样例接口却只认 population，
    未标注桶的计数配上的是 codex 桶的样例。
    """

    @staticmethod
    def _seed(agent_id, harness):
        async def go():
            async with get_session("sqlite+aiosqlite://") as session:
                session.add(
                    AgentModel(
                        id=agent_id,
                        team_id="t1",
                        name=agent_id,
                        role="worker",
                        transcript_path=None,
                        harness=harness,
                        created_at=utc_now(),
                    )
                )

        _run(go())

    @staticmethod
    def _ids(resp):
        assert resp.status_code == 200, resp.text
        samples = resp.json()["data"]["samples"]
        return {(r["agent_id"], r["harness"]) for rows in samples.values() for r in rows}

    def test_three_values_select_three_different_buckets(self, integration_client):
        self._seed("cc-1", None)
        self._seed("codex-1", "codex")
        get = integration_client.get

        assert self._ids(get("/api/usage/unattributed?harness=unlabelled")) == {("cc-1", None)}
        assert self._ids(get("/api/usage/unattributed?harness=codex")) == {("codex-1", "codex")}
        both = {("cc-1", None), ("codex-1", "codex")}
        assert self._ids(get("/api/usage/unattributed")) == both, "缺省必须是不过滤"
        assert self._ids(get("/api/usage/unattributed?harness=*")) == both

    def test_empty_harness_is_refused_not_read_as_a_bucket(self, integration_client):
        resp = integration_client.get("/api/usage/unattributed?harness=")
        assert resp.status_code == 400
        assert "unlabelled" in resp.json()["detail"]

    def test_sentinels_can_never_collide_with_a_real_harness(self):
        from aiteam.api.routes.usage import UNLABELLED_HARNESS_PARAM
        from aiteam.storage.repository import ANY_HARNESS
        from aiteam.types import HarnessId

        real = {h.value for h in HarnessId}
        assert UNLABELLED_HARNESS_PARAM not in real
        assert ANY_HARNESS not in real
        assert UNLABELLED_HARNESS_PARAM != ANY_HARNESS

    def test_dashboard_sends_the_same_unlabelled_literal(self):
        """前端与后端各写一份字面量：拼错一个字，未标注桶的请求会被当成一个叫这个名字
        的 harness，返回空样例而不报错。"""
        from pathlib import Path

        from aiteam.api.routes.usage import UNLABELLED_HARNESS_PARAM

        usage_ts = Path(__file__).resolve().parents[2] / "dashboard/src/api/usage.ts"
        assert f"const UNLABELLED_HARNESS = '{UNLABELLED_HARNESS_PARAM}';" in usage_ts.read_text()


class TestCodexSingleProbe:
    """单次实测对 Codex 行走 rollout 解析器（任务 06f5ee0e (c)）：按 CC transcript 读
    rollout 只会得到"没有用量行"，再被报成 transcript_gone，文件明明在。"""

    @staticmethod
    def _rollout(home, thread, *, usage=True, cache_write=True):
        import json

        folder = home / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-09-30-{thread}.jsonl"
        rows = [dict(timestamp="2026-09-30T00:00:00Z", type="session_meta", payload=dict(id=thread, source="cli"))]
        if usage:
            raw = dict(input_tokens=100, cached_input_tokens=60, cache_write_input_tokens=0, output_tokens=20,
                       reasoning_output_tokens=5, total_tokens=120)
            if not cache_write:
                raw.pop("cache_write_input_tokens")
            rows.append(dict(timestamp="2026-09-30T00:00:01Z", type="token_usage_record",
                             payload=dict(response_id="r1", thread_id=thread, usage=raw)))
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    @staticmethod
    def _seed(agent_id, *, role="worker", session_id=None, cc_tool_use_id=None, transcript_path=None):
        async def go():
            async with get_session("sqlite+aiosqlite://") as session:
                session.add(
                    AgentModel(
                        id=agent_id, team_id="t1", name=agent_id, role=role, harness="codex",
                        session_id=session_id, cc_tool_use_id=cc_tool_use_id,
                        transcript_path=str(transcript_path) if transcript_path else None,
                        created_at=utc_now(),
                    )
                )

        _run(go())

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        home = tmp_path / "codex-home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CODEX_HOME", str(home))
        return home

    def test_child_is_measured_from_its_own_rollout(self, integration_client, home):
        path = self._rollout(home, "child-thread")
        self._rollout(home, "root-thread", usage=False)
        self._seed("child", session_id="root-thread", cc_tool_use_id="child-thread", transcript_path=path)
        resp = integration_client.get("/api/usage/probe?agent_id=child")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert (data["input_tokens"], data["output_tokens"], data["cache_read_tokens"]) == (40, 20, 60)
        assert (data["cache_creation_tokens"], data["api_calls"], data["transcript_path"]) == (0, 1, str(path))
        assert "total_tokens" not in data

    def test_leader_without_a_recorded_path_is_found_by_thread_id(self, integration_client, home):
        path = self._rollout(home, "leader-thread", cache_write=False)
        self._seed("leader", role="leader", session_id="leader-thread")
        resp = integration_client.get("/api/usage/probe?agent_id=leader")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["transcript_path"] == str(path)
        # The rollout has no cache-write field: not recorded, never reported as 0.
        assert data["cache_creation_tokens"] is None
        assert data["input_tokens"] == 40

    def test_no_usage_event_missing_rollout_and_refusal_are_told_apart(self, integration_client, home):
        self._seed("empty", role="leader", session_id="empty-thread",
                   transcript_path=self._rollout(home, "empty-thread", usage=False))
        self._seed("missing", role="leader", session_id="missing-thread")
        conflicted = self._rollout(home, "bad-thread")
        # The same request id recorded twice with different values: the parser refuses.
        conflicted.write_text(conflicted.read_text() + conflicted.read_text().splitlines()[1].replace(
            '"output_tokens": 20, "reasoning_output_tokens": 5, "total_tokens": 120',
            '"output_tokens": 30, "reasoning_output_tokens": 5, "total_tokens": 130') + "\n")
        self._seed("refused", role="leader", session_id="bad-thread", transcript_path=conflicted)
        self._seed("nameless", cc_tool_use_id=None)

        empty = integration_client.get("/api/usage/probe?agent_id=empty")
        assert (empty.status_code, "no_usage_lines" in empty.json()["detail"]) == (404, True)
        missing = integration_client.get("/api/usage/probe?agent_id=missing")
        assert (missing.status_code, "transcript_gone" in missing.json()["detail"]) == (404, True)
        refused = integration_client.get("/api/usage/probe?agent_id=refused")
        assert (refused.status_code, "conflict" in refused.json()["detail"]) == (422, True)
        assert integration_client.get("/api/usage/probe?agent_id=nameless").status_code == 404
