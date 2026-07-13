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
    trust,
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
    loop,
    pipeline,
    analytics,
    links,
    reports,
    briefing,
    scheduler,
    task_analysis,
    memory,
    infra,
    file_lock,
    git_ops,
    channels,
    guardrails,
    trust,
    watchdog,
    error_budget_tool,
    ecosystem,
    workflows,
]

# ============================================================
# Tool tier definitions
# Purpose: document cognitive load grouping for future optimization.
# Currently all tools are registered by default (CORE + ADVANCED).
# When CC context budgets become a constraint, ADVANCED tools can be
# gated behind a tools_load_advanced() call.
# ============================================================

# ~15 essential tools an Agent needs every session
CORE_TOOLS: list[str] = [
    # Task management
    "task_create",
    "task_update",
    "task_status",
    "task_list",
    "task_memo_add",
    "task_memo_read",
    # Team & agent awareness
    "team_list",
    "context_resolve",
    "taskwall_view",
    # Memory & knowledge
    "memory_search",
    "memory_add",
    "team_knowledge",
    # Infrastructure
    "report_save",
    "send_message",
    "health_check",
]

# All remaining tools — domain-specific, used when relevant
ADVANCED_TOOLS: list[str] = [
    # Analytics & metrics
    "analytics_summary",
    "activity_log",
    # Agent & team management
    "agent_create",
    "agent_update",
    "agent_delete",
    "team_create",
    "team_update",
    "team_delete",
    # Loop & retrospective
    "loop_start",
    "loop_end",
    "loop_status",
    "loop_review",
    # Meetings & decisions
    "meeting_create",
    "meeting_update",
    "decision_record",
    "decision_list",
    # Briefings
    "briefing_create",
    "briefing_list",
    # Pipeline
    "pipeline_run",
    "pipeline_status",
    # Scheduler
    "scheduler_add",
    "scheduler_list",
    "scheduler_remove",
    # Task analysis & execution patterns
    "task_analysis_run",
    "pattern_record",
    "pattern_search",
    # File lock
    "file_lock_acquire",
    "file_lock_release",
    "file_lock_status",
    # Git operations
    "git_commit",
    "git_status",
    "git_diff",
    # Channels & messaging
    "channel_send",
    "channel_list",
    # Guardrails
    "guardrail_check",
    # Reports
    "report_list",
    "report_get",
    # Project management
    "project_create",
    "project_list",
    "project_get",
    # Prompt registry
    "prompt_get",
    "prompt_list",
    # Settings
    "settings_get",
    "settings_set",
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
      - 缺省无 env → 全部 24 组共 166 工具注册（向后兼容）；
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
