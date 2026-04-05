"""MCP tool modules — each module exposes a register(mcp) function."""

from __future__ import annotations

from aiteam.mcp.tools import (
    agent,
    analytics,
    briefing,
    cache,
    channels,
    file_lock,
    git_ops,
    guardrails,
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
    trust,
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
    file_lock,
    git_ops,
    channels,
    guardrails,
    cache,
    trust,
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
    "memory_store",
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
    # Cache management
    "cache_stats",
    "cache_clear",
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


def register_all(mcp) -> None:
    """Register all tool modules on the given FastMCP instance.

    All tools (CORE + ADVANCED) are registered by default.
    The CORE_TOOLS / ADVANCED_TOOLS lists above are informational tier
    definitions for future context-budget optimization.
    """
    for module in _MODULES:
        module.register(mcp)
