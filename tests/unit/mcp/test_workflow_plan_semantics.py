"""workflow 观测「实际 vs 计划」语义的投影回归（任务 cc62ba76）。

事故语境：``planned_agent_count`` 只是静态解析出的**下限**（脚本里字面的 agent()
调用数），动态扇出（pipeline / .map / while 跑运行时数组）会乘出多少个 agent 静态
不可知。真实样本 ic-design-doc-sweep 计划 2 / 实际 8，被读成"算错了"。

解析器早就产出了 ``dynamic_nodes`` 并入库，但 MCP 投影没带出来——会话里只看得到
"8 / 2"，缺了那句"因为有 3 个动态扇出节点"。这里钉死：**行投影必须同时带出
dynamic_nodes**，否则读者无从判断超出是否正常。

替身不比生产宽松：夹具行由真 ``WorkflowRun`` 模型 ``model_dump(mode="json")``
生成，字段集与 ``GET /api/workflows`` 同源。
"""

from __future__ import annotations

import pytest

from aiteam.mcp.tools import workflows as workflow_tools
from aiteam.types import WorkflowRun


class _Collector:
    """替代 FastMCP 注册器，保留原函数以便直接调用（兼容 @mcp.tool(meta=...)）。"""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *_args, **_kwargs):
        def wrap(fn):
            self.tools[fn.__name__] = fn
            return fn

        return wrap


def _tools(module) -> dict:
    collector = _Collector()
    module.register(collector)
    return collector.tools


def _run_row(**overrides) -> dict:
    """一行运行档案——走真模型序列化，字段集与 /api/workflows 同源。"""
    base = {
        "wf_id": "wf_efe1d742-e92",
        "name": "ic-design-doc-sweep",
        "status": "completed",
        "source": "hook+file",
        "planned_agent_count": 2,
        "dynamic_nodes": 3,
        "agent_count": 8,
        "total_tokens": 123_456,
        "total_tool_calls": 42,
        "duration_ms": 600_000,
    }
    base.update(overrides)
    return WorkflowRun(**base).model_dump(mode="json")


class TestWorkflowListProjection:
    @pytest.fixture
    def tools(self):
        return _tools(workflow_tools)

    def _wire(self, monkeypatch, rows: list[dict]) -> None:
        monkeypatch.setattr(
            workflow_tools,
            "_api_call",
            lambda _m, _p, *_a, **_k: {"success": True, "data": rows, "total": len(rows)},
        )

    def test_row_carries_dynamic_nodes(self, tools, monkeypatch):
        """实际 8 / 计划 2 必须伴随 dynamic_nodes=3，否则读起来像算错。"""
        self._wire(monkeypatch, [_run_row()])
        row = tools["workflow_list"]()["runs"][0]
        assert row["dynamic_nodes"] == 3
        assert row["planned_agent_count"] == 2
        assert row["agent_count"] == 8

    def test_static_run_reports_zero_dynamic_nodes(self, tools, monkeypatch):
        """无动态扇出时字段仍在（0 是结论，不是缺字段）。"""
        self._wire(monkeypatch, [_run_row(dynamic_nodes=0, planned_agent_count=3, agent_count=3)])
        row = tools["workflow_list"]()["runs"][0]
        assert row["dynamic_nodes"] == 0

    def test_missing_field_defaults_to_zero(self, tools, monkeypatch):
        """老库行（升表前）缺列时投影不炸，退化为 0。"""
        row_without = {k: v for k, v in _run_row().items() if k != "dynamic_nodes"}
        self._wire(monkeypatch, [row_without])
        assert tools["workflow_list"]()["runs"][0]["dynamic_nodes"] == 0


class TestWorkflowGetProjection:
    """workflow_get 直接透传 run 档案：compact 只截大文本，标量一个不许掉。"""

    @pytest.fixture
    def tools(self):
        return _tools(workflow_tools)

    def _wire(self, monkeypatch, run: dict) -> None:
        def fake(_method, path, *_a, **_k):
            if path.endswith("/agents"):
                return {"success": True, "data": [], "total": 0}
            return run

        monkeypatch.setattr(workflow_tools, "_api_call", fake)

    def test_compact_keeps_plan_semantics_fields(self, tools, monkeypatch):
        self._wire(monkeypatch, _run_row())
        run = tools["workflow_get"]("wf_efe1d742-e92")["run"]
        assert run["dynamic_nodes"] == 3
        assert run["planned_agent_count"] == 2
        assert run["agent_count"] == 8

    def test_full_view_keeps_them_too(self, tools, monkeypatch):
        self._wire(monkeypatch, _run_row())
        run = tools["workflow_get"]("wf_efe1d742-e92", fields="all")["run"]
        assert run["dynamic_nodes"] == 3
        assert run["planned_agent_count"] == 2
