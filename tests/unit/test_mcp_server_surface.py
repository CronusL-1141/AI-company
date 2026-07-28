"""MCP server surface — what a tool-search client can actually find.

CC no longer loads every tool schema up front; it searches tool text and loads
what matches. Two things decide whether 112 tools are reachable at all:

  * the server ``instructions`` — the only text describing the server as a
    whole, so it has to name capabilities the way a searcher would phrase them,
    while staying small enough to always be in context;
  * per-parameter descriptions — an undescribed parameter cannot be matched and
    gets guessed at.

These tests pin both. The parameter invariant is also enforced outside pytest by
scripts/check_tool_param_descriptions.py (I9) so a commit cannot skip it.
"""

from __future__ import annotations

import re

import pytest

from aiteam.mcp.server import mcp

# Budget: instructions ride along in every session, so they must stay cheap.
MAX_INSTRUCTIONS_BYTES = 2048

# Capabilities a user/agent would search for. Each entry is a set of synonyms —
# the instructions must offer at least one phrasing per capability.
REQUIRED_CAPABILITIES = {
    "跨会话任务台账": ("跨会话", "任务墙", "台账"),
    "agent 观测归因": ("观测", "归因", "失败"),
    "记忆治理": ("记忆",),
    "项目隔离": ("项目隔离", "互不", "隔离"),
    "报告知识库": ("报告",),
}


@pytest.fixture(scope="module")
def instructions() -> str:
    text = mcp.instructions
    assert text, "MCP server 必须有 instructions —— 这是 tool search 唯一的服务器级描述"
    return text


@pytest.fixture(scope="module")
def tool_names() -> set[str]:
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    return {t.name for t in tools}


def test_instructions_fit_the_context_budget(instructions):
    size = len(instructions.encode("utf-8"))
    assert size <= MAX_INSTRUCTIONS_BYTES, (
        f"instructions {size} 字节，超出 {MAX_INSTRUCTIONS_BYTES} 字节预算"
    )


def test_instructions_are_capability_oriented(instructions):
    """A one-line label is not searchable; the differentiators must be spelled out."""
    assert len(instructions.encode("utf-8")) > 400, (
        "instructions 过短 —— 一行标签无法支撑按能力检索"
    )
    missing = [
        cap
        for cap, synonyms in REQUIRED_CAPABILITIES.items()
        if not any(s in instructions for s in synonyms)
    ]
    assert not missing, f"instructions 未覆盖核心能力: {missing}"


def test_instructions_only_advertise_real_tools(instructions, tool_names):
    """Naming a tool that does not exist sends the searcher down a dead end."""
    # snake_case identifiers of >=2 segments, plus `family_*` wildcards.
    cited = set(re.findall(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", instructions))
    wildcards = {m for m in re.findall(r"\b([a-z][a-z0-9_]*)_\*", instructions)}
    unknown = sorted(
        name
        for name in cited
        if name not in tool_names
        and not any(name.startswith(f"{w}_") for w in wildcards)
    )
    assert not unknown, f"instructions 提到了不存在的工具: {unknown}"


def test_cited_tool_families_are_non_empty(instructions, tool_names):
    """`ecosystem_*` style shorthand must resolve to tools that actually exist."""
    for family in re.findall(r"\b([a-z][a-z0-9_]*)_\*", instructions):
        prefix = f"{family}_"
        assert any(n.startswith(prefix) for n in tool_names), (
            f"instructions 引用的工具族 {prefix}* 没有任何实际工具"
        )


def test_every_tool_and_parameter_is_described(tool_names):
    """I9 invariant, mirrored in pytest so `pytest tests` alone would catch it."""
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    assert tools, "枚举到 0 个工具 —— register_all 未生效"

    undescribed_tools = [t.name for t in tools if not (t.description or "").strip()]
    assert not undescribed_tools, f"工具缺 description: {undescribed_tools}"

    missing = [
        f"{t.name}.{param}"
        for t in tools
        for param, spec in (t.parameters or {}).get("properties", {}).items()
        if not (spec.get("description") or "").strip()
    ]
    assert not missing, (
        f"参数缺 description（tool search 搜不到）: {missing}\n"
        "注意 docstring Args 段一行只能写一个参数，`limit / offset: ...` 会被整体丢弃"
    )
