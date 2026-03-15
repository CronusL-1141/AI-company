"""Tests for _check_agent_team_name in send_event.py."""

from __future__ import annotations

import sys
from pathlib import Path

# 将hooks目录加入sys.path以便直接导入send_event
_hooks_dir = str(Path(__file__).resolve().parents[2] / "plugin" / "hooks")
if _hooks_dir not in sys.path:
    sys.path.insert(0, _hooks_dir)

from send_event import _check_agent_team_name


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


def test_agent_without_team_name_warns():
    """有实施关键词但无team_name时应产生warning。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "implement the login feature",
        },
    }
    result = _check_agent_team_name(event)
    assert result is not None
    assert "team_name" in result
    assert "WARNING" in result


def test_agent_without_team_name_chinese_keyword_warns():
    """中文实施关键词也应触发warning。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "实现用户登录模块",
        },
    }
    result = _check_agent_team_name(event)
    assert result is not None
    assert "WARNING" in result


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


def test_agent_no_impl_keywords_no_warning():
    """Agent调用无实施关键词时不应warning（可能只是查询）。"""
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "check the status of the deployment",
        },
    }
    assert _check_agent_team_name(event) is None
