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
