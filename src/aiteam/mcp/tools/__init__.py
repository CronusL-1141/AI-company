"""MCP tool modules — each module exposes a register(mcp) function."""

from __future__ import annotations

from aiteam.mcp.tools import (
    agent,
    analytics,
    briefing,
    infra,
    loop,
    meeting,
    memory,
    pipeline,
    project,
    reports,
    scheduler,
    task,
    task_analysis,
    team,
)

_MODULES = [
    team,
    agent,
    meeting,
    task,
    project,
    loop,
    pipeline,
    analytics,
    reports,
    briefing,
    scheduler,
    task_analysis,
    memory,
    infra,
]


def register_all(mcp) -> None:
    """Register all tool modules on the given FastMCP instance."""
    for module in _MODULES:
        module.register(mcp)
