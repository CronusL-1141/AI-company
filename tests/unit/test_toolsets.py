"""工具分组开关 + 只读档单测（工具渐进式加载 P2）。

覆盖：AITEAM_TOOLSETS 解析（all/default/列表/未知组名警告/混用增量）、
default 组 ≤50 硬顶、AITEAM_READONLY 剔除写工具且保留读工具、缺省全量 168 不变。
"""

from __future__ import annotations

import pytest
from fastmcp import FastMCP
from fastmcp.tools.base import Tool as FastMCPTool

from aiteam.mcp.tools import register_all
from aiteam.mcp.tools.toolsets import (
    ALL_TOOLSETS,
    DEFAULT_TOOLSETS,
    WRITE_TOOLS,
    resolve_readonly,
    resolve_toolsets,
)

# 全量工具数基线——改动此数须同步 docs/CHANGELOG（红线 I6 只认工具计数）。
TOTAL_TOOLS = 168
DEFAULT_HARD_CAP = 50


def _registered_names(monkeypatch, env: dict[str, str] | None = None) -> list[str]:
    """在给定 env 下注册全部模块，返回裸工具名列表。"""
    monkeypatch.delenv("AITEAM_TOOLSETS", raising=False)
    monkeypatch.delenv("AITEAM_READONLY", raising=False)
    for key, val in (env or {}).items():
        monkeypatch.setenv(key, val)
    mcp = FastMCP(name="test")
    register_all(mcp)
    return sorted(
        comp.name
        for comp in mcp.local_provider._components.values()
        if isinstance(comp, FastMCPTool)
    )


# ---------------------------------------------------------------------------
# resolve_toolsets：纯函数解析
# ---------------------------------------------------------------------------


def test_resolve_none_is_all() -> None:
    assert resolve_toolsets(None) == set(ALL_TOOLSETS)


def test_resolve_empty_string_is_all() -> None:
    assert resolve_toolsets("") == set(ALL_TOOLSETS)


def test_resolve_all_keyword() -> None:
    assert resolve_toolsets("all") == set(ALL_TOOLSETS)
    assert resolve_toolsets("ALL") == set(ALL_TOOLSETS)


def test_resolve_default_keyword() -> None:
    assert resolve_toolsets("default") == set(DEFAULT_TOOLSETS)


def test_resolve_explicit_list() -> None:
    assert resolve_toolsets("task,team") == {"task", "team"}


def test_resolve_list_trims_and_lowercases() -> None:
    assert resolve_toolsets(" Task , TEAM ") == {"task", "team"}


def test_resolve_unknown_name_warns_and_ignored(capsys: pytest.CaptureFixture) -> None:
    result = resolve_toolsets("task,bogus")
    assert result == {"task"}
    err = capsys.readouterr().err
    assert "bogus" in err


def test_resolve_incremental_default_plus_group() -> None:
    # default + 增量组
    result = resolve_toolsets("default,ecosystem")
    assert result == set(DEFAULT_TOOLSETS) | {"ecosystem"}


def test_resolve_all_unknown_falls_back_to_all(
    capsys: pytest.CaptureFixture,
) -> None:
    result = resolve_toolsets("zzz,qqq")
    assert result == set(ALL_TOOLSETS)
    assert "回退" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# resolve_readonly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_resolve_readonly_truthy(raw: str) -> None:
    assert resolve_readonly(raw) is True


@pytest.mark.parametrize("raw", [None, "", "0", "false", "no", "off", "random"])
def test_resolve_readonly_falsy(raw) -> None:
    assert resolve_readonly(raw) is False


# ---------------------------------------------------------------------------
# 注册期行为
# ---------------------------------------------------------------------------


def test_default_env_registers_full_168(monkeypatch: pytest.MonkeyPatch) -> None:
    """无任何 env → 全量 168 工具，向后兼容不变。"""
    names = _registered_names(monkeypatch)
    assert len(names) == TOTAL_TOOLS


def test_toolsets_all_registers_full_168(monkeypatch: pytest.MonkeyPatch) -> None:
    names = _registered_names(monkeypatch, {"AITEAM_TOOLSETS": "all"})
    assert len(names) == TOTAL_TOOLS


def test_default_group_under_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """default 组注册后工具数硬顶 ≤50（普适护栏 + 官方 30-50 拐点）。"""
    names = _registered_names(monkeypatch, {"AITEAM_TOOLSETS": "default"})
    assert len(names) <= DEFAULT_HARD_CAP
    # 核心组代表工具必须在
    assert "task_create" in names
    assert "task_memo_add" in names
    assert "memory_search" in names


def test_explicit_group_only_registers_that_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = _registered_names(monkeypatch, {"AITEAM_TOOLSETS": "task"})
    assert all(n.startswith("task") or n == "taskwall_view" for n in names)
    # ecosystem 组不该出现
    assert not any(n.startswith("ecosystem_") for n in names)


def test_incremental_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _registered_names(monkeypatch, {"AITEAM_TOOLSETS": "default"})
    inc = _registered_names(
        monkeypatch, {"AITEAM_TOOLSETS": "default,ecosystem"}
    )
    assert len(inc) > len(base)
    assert any(n.startswith("ecosystem_") for n in inc)


# ---------------------------------------------------------------------------
# 只读档
# ---------------------------------------------------------------------------


def test_readonly_removes_write_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """全量 + 只读 → 写工具全部剔除，读工具全部保留。"""
    full = set(_registered_names(monkeypatch))
    ro = set(_registered_names(monkeypatch, {"AITEAM_READONLY": "1"}))
    # 剔除量 == 全量里命中 WRITE_TOOLS 的数量
    assert full - ro == (full & WRITE_TOOLS)
    # 写工具一个不剩
    assert not (ro & WRITE_TOOLS)
    # 代表性写工具确实没了
    for w in ("task_create", "task_update", "git_auto_commit", "os_restart_api"):
        assert w not in ro
    # 代表性读工具仍在
    for r in ("task_list_project", "task_memo_read", "task_status", "team_list"):
        assert r in ro


def test_readonly_orthogonal_to_toolsets(monkeypatch: pytest.MonkeyPatch) -> None:
    """只读与分组正交叠加：default + 只读。"""
    ro = set(
        _registered_names(
            monkeypatch,
            {"AITEAM_TOOLSETS": "default", "AITEAM_READONLY": "1"},
        )
    )
    assert "task_create" not in ro  # 写，剔除
    assert "task_memo_add" not in ro  # 写，剔除
    assert "task_memo_read" in ro  # 读，保留
    assert not (ro & WRITE_TOOLS)


def test_write_tools_all_exist_in_full_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WRITE_TOOLS 清单不得含幽灵名（防工具改名后清单失效）。"""
    full = set(_registered_names(monkeypatch))
    ghosts = WRITE_TOOLS - full
    assert not ghosts, f"WRITE_TOOLS 含已不存在的工具：{ghosts}"
