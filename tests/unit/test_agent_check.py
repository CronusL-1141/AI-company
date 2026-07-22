"""Tests for _check_agent_team_name in workflow_reminder.py.

2026-07-22 拦截退役版（缔造者裁定「全面放开+一律自动追踪」，任务 8705dac2）：
无条件硬拦删除——无 team_name 的实施型直派放行（SubagentStart 自动收编）。
保留：explore/plan+team_name 误用提醒；显式 team_name 跨项目拦截（mock 层验证）。
"""

from __future__ import annotations

import sys
import unittest.mock

from aiteam.hooks.workflow_reminder import _check_agent_team_name


def test_agent_with_team_name_no_warning():
    """有 team_name 且跨项目检查通过时不产生 warning。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "create the auth module",
            "team_name": "my-team",
        },
    }
    with unittest.mock.patch(
        "aiteam.hooks.workflow_reminder._check_team_cross_project", return_value=None
    ):
        assert _check_agent_team_name(event) is None


def test_impl_agent_without_team_name_allowed():
    """实施型无 team_name → 放行（旧硬拦已退役，SubagentStart 自动收编）。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "implement the login feature",
        },
    }
    assert _check_agent_team_name(event) is None


def test_impl_agent_chinese_keyword_allowed():
    """中文实施关键词无 team_name → 同样放行。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "实现用户登录模块",
        },
    }
    assert _check_agent_team_name(event) is None


def test_agent_with_name_only_allowed():
    """仅有 name 无 team_name → 放行（自动收编覆盖追踪）。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "implement the login feature",
            "name": "backend-dev",
        },
    }
    assert _check_agent_team_name(event) is None


def test_explore_agent_no_warning():
    """Explore 无 team_name 正常放行。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "explore the codebase and find auth related files",
            "subagent_type": "explore",
        },
    }
    assert _check_agent_team_name(event) is None


def test_explore_agent_with_team_name_warns():
    """Explore + team_name → 误用提醒（内置只读类型不支持 SendMessage）。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "explore the auth flow",
            "subagent_type": "explore",
            "team_name": "my-team",
        },
    }
    warning = _check_agent_team_name(event)
    assert warning is not None and "只读类型" in warning


def test_cross_project_team_blocked():
    """显式 team_name 命中跨项目 → 仍 exit(2)（2026-05-08 事故防线保留）。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "scan repos",
            "team_name": "other-project-team",
        },
    }
    with unittest.mock.patch(
        "aiteam.hooks.workflow_reminder._check_team_cross_project",
        return_value="[OS BLOCK] 跨项目派发被拦截",
    ):
        with unittest.mock.patch.object(sys, "exit") as mock_exit:
            with unittest.mock.patch.object(sys.stderr, "write"):
                _check_agent_team_name(event)
        mock_exit.assert_called_once_with(2)


def test_non_agent_tool_no_warning():
    """非 Agent 工具不检查。"""
    event = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "npm run build",
        },
    }
    assert _check_agent_team_name(event) is None


def test_plain_agent_no_impl_keywords_allowed():
    """无实施关键词、无 team_name → 放行（全面放开后无差别）。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "check the status of the deployment",
        },
    }
    assert _check_agent_team_name(event) is None
