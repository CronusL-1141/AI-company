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
        assert len(warnings) >= 1
        assert any("任务墙" in w for w in warnings)
        assert any("task_run" in w or "task_create" in w for w in warnings)


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
    """超过30分钟未查看任务墙应提醒（催办治理：间隔 900→1800s）。"""

    def test_stale_taskwall_warning(self):
        # 设置last_taskwall_view为35分钟前（超过 1800s 阈值）
        stale_ago = time.time() - 2100
        state = {"last_taskwall_view": stale_ago}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert any("距上次查看任务墙" in w for w in warnings)
        # 提醒后应重置timer
        assert state["last_taskwall_view"] > stale_ago

    def test_no_stale_warning_within_30_minutes(self):
        # 设置last_taskwall_view为20分钟前（在 1800s 阈值内，不再催）
        twenty_min_ago = time.time() - 1200
        state = {"last_taskwall_view": twenty_min_ago}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert not any("距上次查看任务墙" in w for w in warnings)

    def test_no_stale_warning_when_never_viewed(self):
        # last_taskwall_view为0（从未查看），不应产生staleness提醒
        state = {"last_taskwall_view": 0}
        event = {"tool_name": "Bash", "hook_event_name": "PreToolUse"}
        warnings = _check_workflow_reminders(event, state)
        assert not any("距上次查看任务墙" in w for w in warnings)


class TestTaskwallCatchupGovernance:
    """催办治理①：重置事件扩大到全部 task_*/taskwall_* + 会话内催办上限 2 次后静默。"""

    def test_task_create_resets_timer(self):
        # task_create 属任务墙操作，应重置计时器（旧实现只认 view 两工具）
        stale_ago = time.time() - 2100
        state = {"last_taskwall_view": stale_ago}
        event = {
            "tool_name": "mcp__ai-team-os__task_create",
            "hook_event_name": "PostToolUse",
        }
        warnings = _check_workflow_reminders(event, state)
        assert not any("距上次查看任务墙" in w for w in warnings)
        assert state["last_taskwall_view"] > stale_ago

    def test_task_update_and_memo_add_reset_timer(self):
        for tool in (
            "mcp__ai-team-os__task_update",
            "mcp__ai-team-os__task_memo_add",
            "mcp__ai-team-os__task_status",
        ):
            stale_ago = time.time() - 2100
            state = {"last_taskwall_view": stale_ago}
            event = {"tool_name": tool, "hook_event_name": "PostToolUse"}
            warnings = _check_workflow_reminders(event, state)
            assert not any("距上次查看任务墙" in w for w in warnings), tool
            assert state["last_taskwall_view"] > stale_ago, tool

    def test_catchup_silenced_after_two_times_same_session(self):
        # 同一会话催办 2 次后静默；换会话重新计数
        sid = "sess-catchup-1"
        emitted = 0
        state: dict = {}
        for _ in range(4):
            state["last_taskwall_view"] = time.time() - 2100  # 每轮制造超时
            event = {
                "tool_name": "Bash",
                "hook_event_name": "PreToolUse",
                "session_id": sid,
            }
            warnings = _check_workflow_reminders(event, state)
            if any("距上次查看任务墙" in w for w in warnings):
                emitted += 1
        assert emitted == 2, f"应最多催 2 次，实际 {emitted}"

        # 另一会话独立计数，仍能催
        state["last_taskwall_view"] = time.time() - 2100
        event2 = {
            "tool_name": "Bash",
            "hook_event_name": "PreToolUse",
            "session_id": "sess-catchup-2",
        }
        warnings2 = _check_workflow_reminders(event2, state)
        assert any("距上次查看任务墙" in w for w in warnings2)


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


class TestTaskwallToolHelper:
    """_is_taskwall_tool：全部 task_*/taskwall_*（前缀或裸名）判为任务墙操作。"""

    def test_matches_task_and_taskwall(self):
        from aiteam.hooks.workflow_reminder import _is_taskwall_tool

        for name in (
            "mcp__ai-team-os__task_create",
            "mcp__ai-team-os__task_update",
            "mcp__ai-team-os__task_memo_add",
            "mcp__ai-team-os__task_status",
            "mcp__ai-team-os__taskwall_view",
            "task_list_project",
            "taskwall_view",
        ):
            assert _is_taskwall_tool(name), name

    def test_rejects_non_taskwall(self):
        from aiteam.hooks.workflow_reminder import _is_taskwall_tool

        for name in ("Bash", "SendMessage", "mcp__ai-team-os__team_create", "Read"):
            assert not _is_taskwall_tool(name), name


class TestSessionBucketHelper:
    """_session_bucket：会话隔离 + 24h TTL 剪枝防状态膨胀。"""

    def test_per_session_isolation(self):
        from aiteam.hooks.workflow_reminder import _session_bucket

        state: dict = {}
        b1 = _session_bucket(state, "sess-A")
        b1["flag"] = True
        b2 = _session_bucket(state, "sess-B")
        assert b2.get("flag") is None  # B 看不到 A 的标记
        # 再取 A 应拿回同一桶
        assert _session_bucket(state, "sess-A").get("flag") is True

    def test_stale_buckets_pruned(self):
        from aiteam.hooks.workflow_reminder import _session_bucket

        state: dict = {}
        _session_bucket(state, "old-sess")
        # 手工把 old-sess 打成 25h 前
        state["session_scoped"]["old-sess"]["_ts"] = time.time() - 25 * 3600
        _session_bucket(state, "new-sess")  # 触发剪枝
        assert "old-sess" not in state["session_scoped"]
        assert "new-sess" in state["session_scoped"]


class TestReportFormatDirectionGating:
    """催办治理⑤：汇报格式提醒仅对子agent会话（成员汇报）触发，排除 Leader 会话。"""

    _MSG = {
        "to": "team-lead",
        "message": (
            "任务完成了，我做了很多工作，改了几个文件，也把测试跑了一遍，"
            "但是这条消息故意没有按标准格式写那三个字段，凑够一百字以上以触发校验"
            "啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊"
        ),
    }

    def test_member_session_gets_format_reminder(self):
        event = {
            "tool_name": "SendMessage",
            "tool_input": self._MSG,
            "hook_event_name": "PreToolUse",
            "session_id": "member-sess-1",
        }
        with mock.patch.object(workflow_reminder, "_is_subagent_session", return_value=True):
            warnings = _check_workflow_reminders(event, {})
        assert any("汇报可能缺少标准字段" in w for w in warnings)

    def test_leader_session_excluded(self):
        event = {
            "tool_name": "SendMessage",
            "tool_input": self._MSG,
            "hook_event_name": "PreToolUse",
            "session_id": "leader-sess-1",
        }
        with mock.patch.object(workflow_reminder, "_is_subagent_session", return_value=False):
            warnings = _check_workflow_reminders(event, {})
        assert not any("汇报可能缺少标准字段" in w for w in warnings)

    def test_member_session_throttled_once(self):
        event = {
            "tool_name": "SendMessage",
            "tool_input": self._MSG,
            "hook_event_name": "PreToolUse",
            "session_id": "member-sess-2",
        }
        state: dict = {}
        with mock.patch.object(workflow_reminder, "_is_subagent_session", return_value=True):
            first = _check_workflow_reminders(event, state)
            second = _check_workflow_reminders(event, state)
        assert any("汇报可能缺少标准字段" in w for w in first)
        assert not any("汇报可能缺少标准字段" in w for w in second)


class TestCommitBranchOwnership:
    """A1'(S5) commit 期分支断言：谁在哪个 checkout 上认领了哪条分支。

    裁决语义（辩论 503e07f1 议题A）：状态全落 hook 自身 supervisor state 不调 API；
    首次提交记 (checkout, agent)→branch+ts；自己换分支响亮警告不拦；落到他人 24h
    内的活跃记录硬拦 exit(2)；他人记录过期降为警告（防死 agent 记录误伤合法接手者）；
    git 探测失败 fail-loud（既不放行也不硬拦，显式声明检查未执行）。
    """

    _CHECKOUT = "/repo/main"

    def _event(self, session_id: str, cmd: str = "git commit -m 'x'") -> dict:
        return {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "cwd": self._CHECKOUT,
        }

    def _git(self, branch: str, checkout: str | None = None):
        """Patch the read-only git probe to report a fixed checkout + branch."""
        out = f"{checkout or self._CHECKOUT}\n{branch}"
        return mock.patch.object(
            workflow_reminder, "_run_git_readonly", return_value=(0, out)
        )

    @staticmethod
    def _own(warnings: list[str]) -> list[str]:
        return [w for w in warnings if "分支所有权" in w]

    def _records(self, state: dict) -> dict:
        return state.get("branch_ownership", {}).get(self._CHECKOUT, {})

    def test_first_commit_records_silently(self):
        state: dict = {}
        with self._git("feature-a"):
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        assert self._own(warnings) == []
        rec = self._records(state)["agent-1"]
        assert rec["branch"] == "feature-a"
        assert rec["ts"] > 0

    def test_same_branch_stays_silent(self):
        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
            self._records(state)["agent-1"]["ts"] = time.time() - 600
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        assert self._own(warnings) == []
        # 仍在自己的分支上干活 → 认领续期，不让它自然过期
        assert time.time() - self._records(state)["agent-1"]["ts"] < 5

    def test_self_switch_warns_loudly_without_blocking(self):
        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
        with self._git("feature-b"):
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        own = self._own(warnings)
        assert len(own) == 1
        # 同屏给出记录分支与当前 HEAD
        assert "feature-a" in own[0] and "feature-b" in own[0]

    def test_other_agent_active_claim_hard_blocks(self):
        import pytest

        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
            with pytest.raises(SystemExit) as exc:
                _check_workflow_reminders(self._event("agent-2"), state)
        assert exc.value.code == 2

    def test_other_agent_expired_claim_downgrades_to_warning(self):
        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
            # 记录打成 25h 前：超过活跃 TTL，未到剪枝 TTL
            self._records(state)["agent-1"]["ts"] = time.time() - 25 * 3600
            warnings = _check_workflow_reminders(self._event("agent-2"), state)
        own = self._own(warnings)
        assert len(own) == 1
        assert "过期" in own[0]
        # 合法接手者拿到自己的认领
        assert self._records(state)["agent-2"]["branch"] == "feature-a"

    def test_git_probe_failure_fails_loud(self):
        state: dict = {}
        with mock.patch.object(workflow_reminder, "_run_git_readonly", return_value=(1, "")):
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        own = self._own(warnings)
        assert len(own) == 1
        assert "未能执行" in own[0]
        # 探测不出来就不记账，免得把错的所有权坐实
        assert state.get("branch_ownership", {}) == {}

    def test_detached_head_fails_loud(self):
        state: dict = {}
        with self._git("HEAD"):
            warnings = _check_workflow_reminders(self._event("agent-1"), state)
        assert len(self._own(warnings)) == 1
        assert state.get("branch_ownership", {}) == {}

    def test_non_commit_git_command_ignored(self):
        state: dict = {}
        with self._git("feature-a"):
            for cmd in ("git status", "git log --oneline -3", "git diff HEAD"):
                warnings = _check_workflow_reminders(self._event("agent-1", cmd), state)
                assert self._own(warnings) == []
        assert state.get("branch_ownership", {}) == {}

    def test_post_tool_use_does_not_check(self):
        """提交后再断言毫无意义（木已成舟），只在 PreToolUse 跑。"""
        state: dict = {}
        event = self._event("agent-1")
        event["hook_event_name"] = "PostToolUse"
        with self._git("feature-a"):
            warnings = _check_workflow_reminders(event, state)
        assert self._own(warnings) == []
        assert state.get("branch_ownership", {}) == {}

    def test_stale_records_pruned(self):
        from aiteam.hooks.workflow_reminder import _BRANCH_OWNERSHIP_PRUNE_TTL

        state: dict = {}
        with self._git("feature-a"):
            _check_workflow_reminders(self._event("agent-1"), state)
            self._records(state)["agent-1"]["ts"] = (
                time.time() - _BRANCH_OWNERSHIP_PRUNE_TTL - 60
            )
            _check_workflow_reminders(self._event("agent-9"), state)
        # 过期到剪枝线的记录被整条清掉，state 文件不随 agent 数无限膨胀
        assert "agent-1" not in self._records(state)
        assert "agent-9" in self._records(state)

    def test_separate_checkouts_do_not_collide(self):
        state: dict = {}
        with self._git("feature-a", checkout="/repo/main"):
            _check_workflow_reminders(self._event("agent-1"), state)
        # 另一个 worktree 同名分支不该被当成同一次认领（git 本就不允许，但状态键要分得开）
        with self._git("feature-a", checkout="/repo/wt-1"):
            warnings = _check_workflow_reminders(self._event("agent-2"), state)
        assert self._own(warnings) == []
        assert state["branch_ownership"]["/repo/wt-1"]["agent-2"]["branch"] == "feature-a"
