"""Tests for workflow_reminder.py.

Tests workflow reminder logic: TeamCreate task reminder, Agent memo reminder,
shutdown completion reminder, taskwall staleness warning, and cooldowns.
"""

from __future__ import annotations

import os
import time
from unittest import mock

import aiteam.hooks.workflow_reminder as workflow_reminder
from aiteam.hooks.workflow_reminder import _check_workflow_reminders


def _use_temp_state(tmp_path: str):
    """Patch supervisor state file to use a temp directory."""
    state_file = os.path.join(tmp_path, "supervisor-state.json")
    return (
        mock.patch.object(workflow_reminder, "_SUPERVISOR_STATE_FILE", state_file),
        mock.patch.object(workflow_reminder, "_SUPERVISOR_STATE_DIR", tmp_path),
    )


class TestTeamCreateRemindsTask:
    """TeamCreate后应提醒任务上墙。"""

    def test_teamcreate_reminds_task(self):
        state = {}
        event = {"tool_name": "TeamCreate", "hook_event_name": "PostToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert len(warnings) == 1
        assert "任务墙" in warnings[0]
        assert "task_run" in warnings[0] or "task_create" in warnings[0]


class TestAgentRemindsMemo:
    """Agent(team_name)创建前应提醒查看memo。"""

    def test_agent_reminds_memo(self):
        state = {"last_memo_reminder": 0}
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "实现功能", "team_name": "dev-team"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        # Rule 2 now generates multiple warnings: task wall check, template reminder, memo reminder
        assert any("task_memo_read" in w for w in warnings)
        assert state["last_memo_reminder"] > 0

    def test_agent_without_team_name_no_memo_reminder(self):
        state = {"last_memo_reminder": 0}
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "探索代码", "subagent_type": "explore"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        # No team_name in input, so no memo reminder
        assert not any("task_memo_read" in w for w in warnings)


class TestShutdownRemindsComplete:
    """SendMessage(shutdown)应提醒标记任务完成。"""

    def test_shutdown_reminds_complete(self):
        state = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "dev-agent", "message": "shutdown"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        # Rule 3 shutdown reminder + possible Rule 6 parallel task reminder
        assert any("task_memo_add" in w for w in warnings)
        assert any("完成" in w or "标记" in w for w in warnings)

    def test_normal_sendmessage_no_shutdown_warning(self):
        state = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "dev-agent", "message": "请继续工作"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        assert not any("关闭Agent" in w for w in warnings)


class TestTaskwallViewResetsTimer:
    """taskwall_view应重置计时器。"""

    def test_taskwall_view_resets_timer(self):
        state = {"last_taskwall_view": 0}
        event = {"tool_name": "taskwall_view", "hook_event_name": "PostToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert state["last_taskwall_view"] > 0
        # taskwall_view本身不应产生staleness warning
        assert not any("距上次查看任务墙" in w for w in warnings)


class TestStaleTaskwallWarning:
    """超过15分钟未查看任务墙应提醒。"""

    def test_stale_taskwall_warning(self):
        # 设置last_taskwall_view为20分钟前
        twenty_min_ago = time.time() - 1200
        state = {"last_taskwall_view": twenty_min_ago}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert any("距上次查看任务墙" in w for w in warnings)
        # 提醒后应重置timer
        assert state["last_taskwall_view"] > twenty_min_ago

    def test_no_stale_warning_within_15_minutes(self):
        # 设置last_taskwall_view为5分钟前（在15分钟内）
        five_min_ago = time.time() - 300
        state = {"last_taskwall_view": five_min_ago}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert not any("距上次查看任务墙" in w for w in warnings)

    def test_no_stale_warning_when_never_viewed(self):
        # last_taskwall_view为0（从未查看），不应产生staleness提醒
        state = {"last_taskwall_view": 0}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert not any("距上次查看任务墙" in w for w in warnings)


class TestMemoReminderCooldown:
    """5分钟冷却内不应重复提醒查看memo。"""

    def test_memo_reminder_cooldown(self):
        # 第一次触发
        state = {"last_memo_reminder": 0}
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "实现功能", "team_name": "dev-team"},
            "hook_event_name": "PreToolUse",
        }
        warnings1 = _check_workflow_reminders(event, state)
        assert any("task_memo_read" in w for w in warnings1)

        # 立即再次触发（冷却内）
        warnings2 = _check_workflow_reminders(event, state)
        assert not any("task_memo_read" in w for w in warnings2)

    def test_memo_reminder_after_cooldown(self):
        # 设置last_memo_reminder为6分钟前（超过5分钟冷却）
        six_min_ago = time.time() - 360
        state = {"last_memo_reminder": six_min_ago}
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "实现功能", "team_name": "dev-team"},
            "hook_event_name": "PreToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        assert any("task_memo_read" in w for w in warnings)


class TestNoWarningNormalFlow:
    """正常流程不应产生多余提醒。"""

    def test_no_warning_normal_flow(self):
        state = {}
        # 普通工具调用不应产生workflow提醒
        for tool in ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]:
            event = {"tool_name": tool, "hook_event_name": "PreToolUse"}
            warnings = _check_workflow_reminders(event, state)
            assert warnings == [], f"Unexpected warning for {tool}: {warnings}"
