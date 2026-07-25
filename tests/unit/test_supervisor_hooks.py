"""Tests for supervisor hook checks in workflow_reminder.py.

Tests _check_leader_doing_too_much and _check_team_has_permanent_members.
"""

from __future__ import annotations

from aiteam.hooks.workflow_reminder import (
    _LEADER_CONSECUTIVE_THRESHOLD,
    _check_leader_doing_too_much,
)


class TestLeaderDoingTooMuch:
    """Tests for _check_leader_doing_too_much."""

    def test_leader_consecutive_calls_warning(self):
        """超过阈值次连续非委派调用时应产生warning。"""
        state: dict = {}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}

        # 前threshold次不应warning
        for i in range(1, _LEADER_CONSECUTIVE_THRESHOLD + 1):
            result = _check_leader_doing_too_much(event, state)
            assert result is None, f"Unexpected warning at call {i}"

        # 第threshold+1次应warning
        result = _check_leader_doing_too_much(event, state)
        assert result is not None
        assert "B0.9" in result
        assert str(_LEADER_CONSECUTIVE_THRESHOLD + 1) in result

    def test_agent_call_resets_counter(self):
        """Agent调用应重置计数器。"""
        state: dict = {}
        bash_event = {"tool_name": "Edit", "hook_event_name": "PreToolUse"}
        agent_event = {"tool_name": "Agent", "hook_event_name": "PreToolUse"}

        # 积累一些调用
        for _ in range(7):
            _check_leader_doing_too_much(bash_event, state)

        # Agent调用重置计数器
        result = _check_leader_doing_too_much(agent_event, state)
        assert result is None
        assert state.get("leader_consecutive_calls", 0) == 0

        # 重新积累不应立即warning
        result = _check_leader_doing_too_much(bash_event, state)
        assert result is None

    def test_workflow_call_resets_counter(self):
        """Workflow(ultracode 委派)调用应重置计数器——用 CC 工作流委派不该被催'为什么不委派'。"""
        state: dict = {}
        bash_event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        workflow_event = {"tool_name": "Workflow", "hook_event_name": "PreToolUse"}

        for _ in range(7):
            _check_leader_doing_too_much(bash_event, state)

        result = _check_leader_doing_too_much(workflow_event, state)
        assert result is None
        assert state.get("leader_consecutive_calls", 0) == 0

    def test_workflow_resets_counter(self):
        """Workflow 编排也是委派动作，应重置计数器。

        （原 test_team_create_resets_counter：TeamCreate 工具于 CC v2.1.219 已不存在
        ——2026-07-25 工具面核对后从 _DELEGATION_TOOLS 移除，测试改测仍在编制内的
        Workflow；Agent 直派的重置另有 test_agent_dispatch_resets_counter 覆盖。）
        """
        state: dict = {}
        bash_event = {"tool_name": "Read", "hook_event_name": "PreToolUse"}
        wf_event = {"tool_name": "Workflow", "hook_event_name": "PreToolUse"}

        for _ in range(7):
            _check_leader_doing_too_much(bash_event, state)

        result = _check_leader_doing_too_much(wf_event, state)
        assert result is None
        assert state.get("leader_consecutive_calls", 0) == 0

    def test_send_message_resets_counter(self):
        """SendMessage调用也应重置计数器。"""
        state: dict = {}
        bash_event = {"tool_name": "Write", "hook_event_name": "PreToolUse"}
        msg_event = {"tool_name": "SendMessage", "hook_event_name": "PreToolUse"}

        for _ in range(7):
            _check_leader_doing_too_much(bash_event, state)

        result = _check_leader_doing_too_much(msg_event, state)
        assert result is None
        assert state.get("leader_consecutive_calls", 0) == 0

    def test_empty_tool_name_no_warning(self):
        """空tool_name时不应处理。"""
        state: dict = {}
        event = {"tool_name": "", "hook_event_name": "PreToolUse"}
        result = _check_leader_doing_too_much(event, state)
        assert result is None

    def test_counter_persists_in_state(self):
        """计数器应在state字典中持久化。"""
        state: dict = {}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}

        _check_leader_doing_too_much(event, state)
        _check_leader_doing_too_much(event, state)
        _check_leader_doing_too_much(event, state)

        assert state["leader_consecutive_calls"] == 3


class TestNormalFlowNoWarning:
    """Tests that normal (well-behaved) flows produce no warnings."""

    def test_normal_flow_no_warning(self):
        """正常流程（混合委派和直接操作）不应产生warning。"""
        state: dict = {}
        # Leader做几步操作
        for tool in ["Read", "Bash", "Read"]:
            event = {"tool_name": tool, "hook_event_name": "PreToolUse"}
            r1 = _check_leader_doing_too_much(event, state)
            assert r1 is None

        # 然后委派
        event = {"tool_name": "Agent", "hook_event_name": "PreToolUse"}
        r1 = _check_leader_doing_too_much(event, state)
        assert r1 is None

        # 再做几步
        for tool in ["Edit", "Bash"]:
            event = {"tool_name": tool, "hook_event_name": "PreToolUse"}
            r1 = _check_leader_doing_too_much(event, state)
            assert r1 is None
