"""Tests for _check_agent_team_name in workflow_reminder.py."""

from __future__ import annotations

import sys

from aiteam.hooks.workflow_reminder import _check_agent_team_name


def test_agent_with_team_name_no_warning():
    """有team_name时不应产生warning。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "create the auth module",
            "team_name": "my-team",
        },
    }
    assert _check_agent_team_name(event) is None


def test_agent_without_team_name_exits():
    """Impl keywords without team_name/name → exit(2) hard block."""
    import unittest.mock

    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "implement the login feature",
        },
    }
    with unittest.mock.patch.object(sys, "exit") as mock_exit:
        with unittest.mock.patch.object(sys.stderr, "write"):
            _check_agent_team_name(event)
    mock_exit.assert_called_once_with(2)


def test_agent_without_team_name_chinese_keyword_exits():
    """Chinese impl keywords without team_name/name → exit(2) hard block."""
    import unittest.mock

    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "实现用户登录模块",
        },
    }
    with unittest.mock.patch.object(sys, "exit") as mock_exit:
        with unittest.mock.patch.object(sys.stderr, "write"):
            _check_agent_team_name(event)
    mock_exit.assert_called_once_with(2)


def test_agent_with_name_only_still_blocked():
    """name alone is not enough — must have explicit team_name."""
    import unittest.mock

    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "implement the login feature",
            "name": "backend-dev",
        },
    }
    with unittest.mock.patch.object(sys, "exit") as mock_exit:
        with unittest.mock.patch.object(sys.stderr, "write"):
            _check_agent_team_name(event)
    mock_exit.assert_called_once_with(2)


def test_explore_agent_no_warning():
    """Explore类型的agent不需要team_name。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "explore the codebase and find auth related files",
            "subagent_type": "explore",
        },
    }
    assert _check_agent_team_name(event) is None


def test_plan_agent_no_warning():
    """Plan类型的agent不需要team_name。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "create a plan for implementing the feature",
            "subagent_type": "plan",
        },
    }
    assert _check_agent_team_name(event) is None


def test_reviewer_agent_no_warning():
    """Reviewer类型的agent不需要team_name。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "review this code for security issues",
            "subagent_type": "code-reviewer",
        },
    }
    assert _check_agent_team_name(event) is None


def test_non_agent_tool_no_warning():
    """非Agent工具不应检查team_name。"""
    event = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "npm run build",
        },
    }
    assert _check_agent_team_name(event) is None


def test_agent_no_impl_keywords_still_blocked():
    """Local agent without impl keywords is also blocked (no team_name/name)."""
    import unittest.mock

    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "check the status of the deployment",
        },
    }
    with unittest.mock.patch.object(sys, "exit") as mock_exit:
        with unittest.mock.patch.object(sys.stderr, "write"):
            _check_agent_team_name(event)
    mock_exit.assert_called_once_with(2)
