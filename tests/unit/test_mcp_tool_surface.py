"""按生效档位核对真实 FastMCP 目录，注入缺失后必须报告具体工具名。"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP

from aiteam.mcp import tools
from aiteam.mcp.tools import infra, register_all
from aiteam.mcp.tools.surface import ToolSurfaceCheck, _declared_names
from aiteam.mcp.tools.toolsets import ALL_TOOLSETS

MODES = [
    pytest.param(None, None, id="all"),
    pytest.param("task,infra", None, id="partial"),
    pytest.param(None, "1", id="readonly"),
    pytest.param("task,infra", "1", id="partial-readonly"),
]


@pytest.fixture(autouse=True)
def health_dependencies(monkeypatch):
    """The surface check does not need API, database, hook or PID side effects."""
    monkeypatch.delenv("AITEAM_TOOLSETS", raising=False)
    monkeypatch.delenv("AITEAM_READONLY", raising=False)
    monkeypatch.setattr(infra, "_api_call", lambda *a, **kw: {"success": False, "error": "offline"})
    monkeypatch.setattr(infra, "_get_api_url", lambda: "https://example.invalid")
    for name in ("_hook_delivery_section", "_json_integrity_section", "_hook_ingest_section"):
        monkeypatch.setattr(infra, name, lambda *a, **kw: {})
    monkeypatch.setattr(infra, "_usage_coverage_line", lambda: "no data")


def _server(monkeypatch, toolsets=None, readonly=None, *, http=False):
    if toolsets is not None:
        monkeypatch.setenv("AITEAM_TOOLSETS", toolsets)
    if readonly is not None:
        monkeypatch.setenv("AITEAM_READONLY", readonly)
    mcp = FastMCP(name="surface-test")
    register_all(mcp, isolate_http_threads=http)
    return mcp


def _health(mcp):
    health = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "os_health_check")
    return health.fn()


@pytest.mark.parametrize("toolsets,readonly", MODES)
def test_effective_mode_is_complete(monkeypatch, toolsets, readonly):
    mcp = _server(monkeypatch, toolsets, readonly)
    section = _health(mcp)["mcp_tool_surface"]
    assert section["status"] == "complete", section
    assert section["expected_count"] == len(asyncio.run(mcp.list_tools()))
    assert section["registered_count"] == section["expected_count"]
    assert section["missing"] == section["unexpected"] == []
    assert section["readonly"] is (readonly == "1")
    assert set(section["toolsets"]) == (set(toolsets.split(",")) if toolsets else ALL_TOOLSETS)
    assert section["scope"] == "server_catalog"
    assert section["host_exposure"] == "unverified"


@pytest.mark.parametrize("toolsets,readonly", MODES)
def test_each_mode_reports_a_removed_tool(monkeypatch, toolsets, readonly):
    mcp = _server(monkeypatch, toolsets, readonly)
    mcp.local_provider.remove_tool("task_status")
    section = _health(mcp)["mcp_tool_surface"]
    assert section["status"] == "mismatch"
    assert section["missing"] == ["task_status"]
    assert section["unexpected"] == []
    assert section["expected_count"] == section["registered_count"] + 1


def test_expected_inventory_survives_an_omitted_module(monkeypatch):
    monkeypatch.setattr(tools, "_MODULES", [m for m in tools._MODULES if m is not tools.task])
    section = _health(_server(monkeypatch))["mcp_tool_surface"]
    assert section["status"] == "mismatch"
    assert "task_status" in section["missing"]
    assert "task_create" in section["missing"]


def test_unexpected_tool_is_named(monkeypatch):
    mcp = _server(monkeypatch, "task,infra", "1")

    @mcp.tool()
    def stray_tool() -> str:
        return "unexpected"

    section = _health(mcp)["mcp_tool_surface"]
    assert section["status"] == "mismatch"
    assert section["unexpected"] == ["stray_tool"]
    assert section["missing"] == []


def test_readonly_removal_failure_is_visible(monkeypatch):
    monkeypatch.setattr(tools, "_remove_write_tools", lambda mcp: [])
    section = _health(_server(monkeypatch, "task,infra", "1"))["mcp_tool_surface"]
    assert section["status"] == "mismatch"
    assert "task_create" in section["unexpected"]


def test_registration_settings_do_not_follow_later_env_changes(monkeypatch):
    first = _server(monkeypatch, "task,infra", "1")
    monkeypatch.setenv("AITEAM_TOOLSETS", "all")
    monkeypatch.setenv("AITEAM_READONLY", "0")
    second = FastMCP(name="second-instance")
    register_all(second)
    for mcp, readonly, enabled in ((first, True, {"task", "infra"}), (second, False, ALL_TOOLSETS)):
        section = _health(mcp)["mcp_tool_surface"]
        assert section["status"] == "complete"
        assert section["readonly"] is readonly
        assert set(section["toolsets"]) == enabled


def test_api_healthy_branch_keeps_surface_result(monkeypatch):
    monkeypatch.setattr(infra, "_api_call", lambda *a, **kw: {"success": True, "total": 2})
    result = _health(_server(monkeypatch))
    assert result["status"] == "healthy"
    assert result["mcp_tool_surface"]["status"] == "complete"


@pytest.mark.parametrize("http", [False, True], ids=["stdio-worker", "http-worker"])
async def test_real_tool_dispatch_checks_catalog_from_worker(monkeypatch, http):
    mcp = _server(monkeypatch, "task,infra", "1", http=http)
    mcp.local_provider.remove_tool("task_status")
    async with Client(mcp) as client:
        result = await client.call_tool("os_health_check")
    assert result.data["mcp_tool_surface"]["missing"] == ["task_status"]


def test_literal_explicit_name_and_meta_are_recognized():
    source = '''
def register(mcp):
    @mcp.tool(name="renamed", meta={"x": True})
    async def original(): pass
    @mcp.tool
    def bare(): pass
'''
    assert _declared_names(source) == {"renamed", "bare"}


@pytest.mark.parametrize("source", [
    pytest.param("def register(mcp):\n @mcp.tool(name=dynamic)\n def named(): pass", id="dynamic-name"),
    pytest.param("def register(mcp):\n @mcp.tool(enabled=False)\n def disabled(): pass", id="unknown-option"),
    pytest.param("def register(mcp):\n if condition:\n  @mcp.tool()\n  def conditional(): pass", id="conditional"),
    pytest.param(
        "def register(mcp):\n @mcp.tool()\n def valid(): pass\n mcp.tool()(dynamic)", id="dynamic-registration",
    ),
    pytest.param("def register(mcp):\n @other\n @mcp.tool()\n def wrapped(): pass", id="wrapped-tool"),
    pytest.param(
        "def register(mcp):\n @mcp.tool()\n def valid(): pass\n alias = mcp.tool\n alias()(dynamic)",
        id="aliased-registration",
    ),
    pytest.param("def register(mcp): pass", id="empty"),
    pytest.param("invalid python !", id="invalid-source"),
])
def test_unknown_source_shape_never_becomes_complete(monkeypatch, source):
    monkeypatch.setattr("aiteam.mcp.tools.surface.Path.read_text", lambda *a, **kw: source)
    section = _health(_server(monkeypatch))["mcp_tool_surface"]
    assert section["status"] == "unavailable"
    assert section["reason"]


def test_missing_source_is_unavailable(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError("source unavailable")

    monkeypatch.setattr("aiteam.mcp.tools.surface.Path.read_text", missing)
    section = _health(_server(monkeypatch))["mcp_tool_surface"]
    assert section["status"] == "unavailable"
    assert "FileNotFoundError" in section["reason"]


def test_catalog_enumeration_failure_is_unavailable(monkeypatch):
    mcp = _server(monkeypatch)
    health = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "os_health_check").fn

    async def failed():
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(mcp, "list_tools", failed)
    section = health()["mcp_tool_surface"]
    assert section["status"] == "unavailable"
    assert "catalog unavailable" in section["reason"]


async def test_sync_check_on_event_loop_is_explicitly_unavailable():
    check = ToolSurfaceCheck(FastMCP(name="loop-test"), {"infra"}, False)
    assert check.check()["status"] == "unavailable"
