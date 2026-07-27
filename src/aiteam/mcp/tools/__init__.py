"""MCP tool modules — each module exposes a register(mcp) function."""

from __future__ import annotations

import logging
import os

from aiteam.mcp.tools import (
    agent,
    analytics,
    briefing,
    channels,
    ecosystem,
    error_budget_tool,
    file_lock,
    git_ops,
    guardrails,
    infra,
    links,
    meeting,
    memory,
    project,
    reports,
    task,
    task_analysis,
    team,
    watchdog,
    workflows,
)
from aiteam.mcp.tools.toolsets import (
    DEFAULT_TOOLSETS,
    WRITE_TOOLS,
    module_enabled,
    resolve_readonly,
    resolve_toolsets,
)

logger = logging.getLogger(__name__)

# 对外暴露供单测/文档引用
__all__ = ["register_all", "DEFAULT_TOOLSETS", "WRITE_TOOLS"]

_MODULES = [
    team,
    agent,
    meeting,
    task,
    project,
    analytics,
    links,
    reports,
    briefing,
    task_analysis,
    memory,
    infra,
    file_lock,
    git_ops,
    channels,
    guardrails,
    watchdog,
    error_budget_tool,
    ecosystem,
    workflows,
]

def _remove_write_tools(mcp) -> list[str]:
    """AITEAM_READONLY 档：注册后从组件表剔除写类工具，返回实际剔除名单。

    写工具用 WRITE_TOOLS 显式清单判定（不靠命名模式猜）。工具装饰器是函数级
    注册，无法在模块 register 时按工具选择，故统一注册完再按名移除——与 P1
    alwaysLoad 同走 local_provider 组件表。任一移除异常静默跳过，不阻断启动。
    """
    removed: list[str] = []
    provider = getattr(mcp, "local_provider", None)
    if provider is None:
        return removed
    try:
        from fastmcp.tools.base import Tool as FastMCPTool

        names = [
            comp.name
            for comp in provider._components.values()  # noqa: SLF001
            if isinstance(comp, FastMCPTool) and comp.name in WRITE_TOOLS
        ]
    except Exception:
        return removed
    for name in names:
        try:
            provider.remove_tool(name)
            removed.append(name)
        except Exception:
            logger.debug("readonly: 剔除写工具 %s 失败", name, exc_info=True)
    return removed


def register_all(mcp) -> None:
    """Register tool modules on the given FastMCP instance.

    分组开关（AITEAM_TOOLSETS）+ 只读档（AITEAM_READONLY）在此注册期生效：
      - 缺省无 env → 全部 20 组共 142 工具注册（向后兼容）；
      - AITEAM_TOOLSETS 选组 → 只注册命中组名的模块；
      - AITEAM_READONLY=1 → 注册后按 WRITE_TOOLS 剔除写工具，只留读工具。
    未注册的工具天然不可调，构成双保险。
    """
    enabled = resolve_toolsets(os.environ.get("AITEAM_TOOLSETS"))
    for module in _MODULES:
        shortname = module.__name__.rsplit(".", 1)[-1]
        if module_enabled(shortname, enabled):
            module.register(mcp)

    if resolve_readonly(os.environ.get("AITEAM_READONLY")):
        removed = _remove_write_tools(mcp)
        if removed:
            logger.info("AITEAM_READONLY: 剔除 %d 个写工具", len(removed))
