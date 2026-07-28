#!/usr/bin/env python3
"""I9 — MCP tool parameter description machine check.

In the tool-search era CC no longer ships every tool schema up front: it matches
a query against tool and parameter text and only then loads the schema. A
parameter with no description is therefore not a documentation nit — it is a
parameter the model cannot search for, guesses at, and gets wrong.

Why runtime introspection instead of an AST scan: the failure mode found by the
2026-07-28 audit is invisible to source-level greps. All ten undescribed
parameters came from *merged* docstring Args entries —

    min_stars / max_stars: Star range filter.
    limit / offset: Pagination.
    suggested_advocate / critic / judge: Default debate roles.

— which read as perfectly documented in the source, but the docstring parser
cannot split one line across several parameters, so every one of them reached
the wire with an empty description. Only the schema FastMCP actually publishes
tells the truth, so this check asks the server for it via ``mcp.list_tools()``.

Usage: python3 scripts/check_tool_param_descriptions.py    (from the repo root)
Exit code: 0 = every tool and parameter documented, 1 = gaps found.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Always check *this* repo's source, never whatever aiteam happens to be installed.
sys.path.insert(0, str(ROOT / "src"))


async def collect() -> tuple[int, int, list[str], dict[str, list[str]]]:
    """Return (tool_count, param_count, tools_without_description, missing_by_tool)."""
    from aiteam.mcp.server import mcp

    tools = await mcp.list_tools()
    param_count = 0
    undocumented_tools: list[str] = []
    missing: dict[str, list[str]] = defaultdict(list)

    for tool in tools:
        if not (tool.description or "").strip():
            undocumented_tools.append(tool.name)
        properties = (tool.parameters or {}).get("properties", {})
        for name, spec in properties.items():
            param_count += 1
            if not (spec.get("description") or "").strip():
                missing[tool.name].append(name)

    return len(tools), param_count, sorted(undocumented_tools), dict(missing)


def main() -> int:
    try:
        tool_count, param_count, undocumented_tools, missing = asyncio.run(collect())
    except Exception as exc:  # import/registration failure is itself a red line
        print(f"❌ 无法枚举 MCP 工具面（server 导入或注册失败）: {exc!r}")
        return 1

    if not tool_count:
        print("❌ 枚举到 0 个 MCP 工具 —— register_all 未生效，检查失去意义")
        return 1

    missing_count = sum(len(v) for v in missing.values())
    if not missing_count and not undocumented_tools:
        print(
            f"✅ MCP 工具面描述完整: {tool_count} 个工具 / {param_count} 个参数，"
            f"全部带 description"
        )
        return 0

    for name in undocumented_tools:
        print(f"❌ 工具 {name}: 缺工具级 description（docstring 为空）")
    for name in sorted(missing):
        params = ", ".join(sorted(missing[name]))
        print(f"❌ 工具 {name}: 参数缺 description —— {params}")

    print(
        f"\n结论: ❌ {tool_count} 个工具 / {param_count} 个参数中，"
        f"{missing_count} 个参数 + {len(undocumented_tools)} 个工具没有描述。"
        "\n修法: 在函数 docstring 的 Args 段为每个参数单独写一行（写清'何时用/什么含义'，"
        "别复述参数名），或用 Field(description=...)。"
        "\n常见坑: 一行写多个参数（形如 `limit / offset: Pagination.`）解析器无法拆分，"
        "看着有描述其实全丢——必须一参一行。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
