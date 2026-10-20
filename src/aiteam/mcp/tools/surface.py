"""Independent declaration inventory for the server's MCP catalog health check."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from aiteam.mcp.tools.toolsets import MODULE_TOOLSET, WRITE_TOOLS, module_enabled


def _declared_names(source: str) -> set[str]:
    """Read declarations, never execute registration or inspect its result.

    Unsupported declaration shapes fail explicitly rather than silently shrinking
    the expected set. The supported decorator options match the current modules.
    """
    tree = ast.parse(source)
    registers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "register"]
    if len(registers) != 1:
        raise ValueError("expected one register function")
    register = registers[0]
    names: set[str] = set()
    decorators: set[int] = set()
    receivers: set[int] = set()
    for node in ast.walk(register):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if not (isinstance(target, ast.Attribute) and target.attr == "tool"
                    and isinstance(target.value, ast.Name) and target.value.id == "mcp"):
                continue
            if node not in register.body:
                raise ValueError(f"conditional/nested tool declaration: {node.name}")
            if len(node.decorator_list) != 1:
                raise ValueError(f"wrapped tool declaration: {node.name}")
            decorators.add(id(decorator))
            receivers.add(id(target.value))
            name = node.name
            if isinstance(decorator, ast.Call):
                if decorator.args or any(k.arg not in {"name", "meta"} for k in decorator.keywords):
                    raise ValueError(f"unsupported tool decorator: {node.name}")
                for keyword in decorator.keywords:
                    if keyword.arg == "name":
                        value = keyword.value
                        if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or not value.value:
                            raise ValueError(f"nonliteral tool name: {node.name}")
                        name = value.value
            if name in names:
                raise ValueError(f"duplicate tool name: {name}")
            names.add(name)
    for node in ast.walk(register):
        if isinstance(node, ast.Name) and node.id == "mcp" and id(node) not in receivers:
            raise ValueError("unsupported registration receiver/alias")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "mcp"
                and id(node) not in decorators):
            raise ValueError(f"unsupported registration call: mcp.{node.func.attr}")
    if not names:
        raise ValueError("no tool declarations")
    return names


def _expected_names(enabled: set[str], readonly: bool) -> frozenset[str]:
    """Use the module grouping inventory independently of the registration loop."""
    names: set[str] = set()
    for module in MODULE_TOOLSET:
        if not module_enabled(module, enabled):
            continue
        declared = _declared_names(Path(__file__).with_name(f"{module}.py").read_text(encoding="utf-8"))
        if names & declared:
            raise ValueError(f"duplicate tool names in module: {module}")
        names.update(declared)
    return frozenset(names - WRITE_TOOLS if readonly else names)


class ToolSurfaceCheck:
    """Per-instance registration settings and an independent expected inventory."""

    def __init__(self, mcp, enabled: set[str], readonly: bool):
        self._mcp = mcp
        self._settings = {"toolsets": sorted(enabled), "readonly": readonly}
        self._enabled = frozenset(enabled)
        self._readonly = readonly

    def check(self) -> dict:
        """Enumerate through FastMCP's public API in the sync tool worker thread."""
        result = {"scope": "server_catalog", "host_exposure": "unverified", **self._settings}
        try:
            # Normal calls run in a worker. Do not create an unawaited coroutine
            # if a caller invokes this synchronous helper on an event loop.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError("catalog check requires the sync tool worker")
            # Source IO/AST work belongs here in the worker, never in
            # register_all(), which may itself run on an application event loop.
            expected = _expected_names(set(self._enabled), self._readonly)
            actual = {tool.name for tool in asyncio.run(self._mcp.list_tools())}
        except Exception as exc:
            return {**result, "status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        return {
            **result,
            "status": "mismatch" if missing or unexpected else "complete",
            "expected_count": len(expected),
            "registered_count": len(actual),
            "missing": missing,
            "unexpected": unexpected,
        }
