"""Tests for workflow_reminder.py: what the hook says, when, and to whom.

main() end to end (PreToolUse only, silent when there is nothing to say, no
shared counters), the per-session Workflow reminder, and S5 branch ownership.
"""

from __future__ import annotations

import io
import json
import os
import time
from unittest import mock

import pytest

import aiteam.hooks.workflow_reminder as workflow_reminder
from aiteam.hooks.workflow_reminder import _check_workflow_reminders


@pytest.fixture
def run_main(tmp_path, monkeypatch, capsys):
    """Run main() in-process the way Claude Code runs the script: event in argv, JSON on stdin.

    Returns (stdout, exit code). HTTP is pointed at a refused port and counted,
    so a test can also assert that a call made no request at all.
    """
    state_file = tmp_path / "supervisor-state.json"
    monkeypatch.setattr(workflow_reminder, "_SUPERVISOR_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(workflow_reminder, "_SUPERVISOR_STATE_FILE", str(state_file))
    monkeypatch.setattr(workflow_reminder, "_hook_deadline", None)
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:9")
    requests: list[str] = []

    def _refused(req, timeout=None):
        requests.append(getattr(req, "full_url", str(req)))
        raise OSError("refused")

    monkeypatch.setattr(workflow_reminder.urllib.request, "urlopen", _refused)

    def _run(event_name: str, payload: dict) -> tuple[str, int]:
        monkeypatch.setattr(workflow_reminder.sys, "argv", ["workflow_reminder.py", event_name])
        raw = json.dumps(dict(payload, cwd=str(tmp_path))).encode("utf-8")
        monkeypatch.setattr(workflow_reminder.sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8"))
        code = 0
        try:
            workflow_reminder.main()
        except SystemExit as exc:
            code = exc.code
        return capsys.readouterr().out, code

    _run.requests = requests
    _run.state_file = state_file
    return _run


def _context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


_SECRET_EDIT = {
    "session_id": "s-pre-post",
    "tool_name": "Edit",
    "tool_input": {"file_path": "/tmp/a.py", "new_string": 'api_key = "abc"'},
}


class TestAdvisoriesPreToolUseOnly:
    """F2: one advisory per tool call. PostToolUse no longer reruns reminders or guards."""

    def test_pre_tool_use_carries_the_advisory(self, run_main):
        out, code = run_main("PreToolUse", _SECRET_EDIT)
        assert code == 0
        assert "硬编码密钥" in _context(out)

    def test_post_tool_use_prints_nothing_and_touches_nothing(self, run_main):
        out, code = run_main("PostToolUse", _SECRET_EDIT)
        assert (out, code) == ("", 0)
        assert not run_main.state_file.exists()
        assert run_main.requests == []

    def test_post_tool_use_never_blocks_after_the_fact(self, run_main):
        """A guard verdict after the command already ran answers nothing."""
        payload = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "git add .env"}}
        assert run_main("PostToolUse", payload) == ("", 0)
        out, code = run_main("PreToolUse", payload)
        assert code == 2 and out == ""


class TestNothingToSayPrintsNothing:
    """#20: no advisory -> no stdout at all (was an empty hookSpecificOutput per call)."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, id="bash"),
            pytest.param({"tool_name": "Edit", "tool_input": {"file_path": "a.py", "new_string": "x = 1"}}, id="edit"),
            pytest.param({"tool_name": "Agent", "tool_input": {"prompt": "x", "model": "opus"}}, id="agent"),
        ],
    )
    def test_quiet_call_prints_nothing(self, run_main, payload):
        out, code = run_main("PreToolUse", dict(payload, session_id="quiet"))
        assert (out, code) == ("", 0)

    def test_quiet_calls_write_no_state(self, run_main):
        """Nothing changed -> no save; the shared file is not rewritten per call."""
        for _ in range(3):
            run_main("PreToolUse", {"session_id": "q", "tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert not run_main.state_file.exists()
        assert run_main.requests == []


class TestNoSharedCounters:
    """D14: nothing in supervisor-state.json is a counter all sessions bump together."""

    _RETIRED = {
        "leader_consecutive_calls": 30,
        "last_taskwall_view": 1.0,
        "bottleneck_check_count": 49,
        "team_cleanup_check_count": 99,
        "last_template_reminder": 1.0,
        "last_memo_reminder": 1.0,
        "pipeline_pending_warnings": 2,
        "ultracode_hint_at": 1.0,
        "last_dispatched_task_id": "t",
        "last_dispatched_task_title": "x",
        "workflow_reminder_at": 1.0,
        "wall_match_reminder_at": 1.0,
    }

    def test_retired_keys_are_dropped_and_the_rest_is_kept(self, run_main):
        kept = {
            "branch_ownership": {"/repo": {"agent-x": {"branch": "b", "ts": time.time()}}},
            "project_id_by_cwd": {"/p": {"id": "proj", "at": time.time()}},
            "session_scoped": {"other": {"_ts": time.time(), "workflow_reminder_shown": True}},
        }
        run_main.state_file.write_text(json.dumps({**self._RETIRED, **kept}))
        run_main("PreToolUse", {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "ls"}})
        final = json.loads(run_main.state_file.read_text())
        assert final == kept

    def test_a_busy_session_leaves_only_per_session_state(self, run_main):
        calls = [
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            {"tool_name": "Edit", "tool_input": {"file_path": "a.py", "new_string": "x = 1"}},
            {"tool_name": "Write", "tool_input": {"file_path": "b.py", "content": "y = 2"}},
            {"tool_name": "Workflow", "tool_input": {"script": "await agent('x', { model: 'opus' })"}},
            {"tool_name": "Agent", "tool_input": {"prompt": "x", "model": "opus", "name": "w1"}},
        ]
        for _ in range(40):
            for call in calls:
                run_main("PreToolUse", dict(call, session_id="busy"))
        final = json.loads(run_main.state_file.read_text())
        assert set(final) <= {"session_scoped", "branch_ownership", "project_id_by_cwd"}, final
        assert not any(type(v) is int for v in final["session_scoped"]["busy"].values())


class TestWorkflowReminderOncePerSession:
    """F6: the Workflow write-back reminder is said once per session, not every 300s."""

    _WF = {"tool_name": "Workflow", "tool_input": {"script": "await agent('x', { model: 'opus' })"}}

    def test_second_workflow_call_in_session_is_silent(self):
        state: dict = {}
        clock = [1_000_000.0]
        event = dict(self._WF, session_id="lead-1")
        with mock.patch.object(workflow_reminder.time, "time", lambda: clock[0]):
            first = _check_workflow_reminders(event, state)
            clock[0] += 600  # well past the old 300s throttle
            second = _check_workflow_reminders(event, state)
        assert any("task_create" in w for w in first)
        assert not any("Workflow 运行已自动追踪" in w for w in second)

    def test_another_session_still_gets_it(self):
        state: dict = {}
        _check_workflow_reminders(dict(self._WF, session_id="lead-1"), state)
        other = _check_workflow_reminders(dict(self._WF, session_id="lead-2"), state)
        assert any("Workflow 运行已自动追踪" in w for w in other)
        assert "workflow_reminder_at" not in state


class TestToolsOutsideTheMatcherHaveNoBranches:
    """D11: the hook is registered for Agent|Bash|Edit|Write|Workflow only.

    Branches for SendMessage, TeamCreate/TeamDelete, meeting_*, task_* and
    ecosystem_* could never run and are gone; none of them says anything or
    calls the API even when invoked directly.
    """

    @pytest.mark.parametrize(
        "tool_name, tool_input",
        [
            ("SendMessage", {"to": "leader", "message": "任务已完成 shutdown " + "x" * 120}),
            ("TeamCreate", {"team_name": "t"}),
            ("TeamDelete", {"team_name": "t"}),
            ("mcp__ai-team-os__meeting_create", {"topic": "t"}),
            ("mcp__ai-team-os__meeting_conclude", {"meeting_id": "m"}),
            ("mcp__ai-team-os__task_status", {"status": "completed"}),
            ("mcp__ai-team-os__ecosystem_scan", {}),
            ("Read", {"file_path": "a.py"}),
        ],
    )
    def test_silent_and_offline(self, tool_name, tool_input):
        event = {"tool_name": tool_name, "tool_input": tool_input, "session_id": "s",
                 "hook_event_name": "PreToolUse"}
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP expected")):
            state: dict = {}
            for _ in range(120):  # past every old "every N calls" cadence
                assert _check_workflow_reminders(event, state) == []
        assert state == {}


class TestNoSubagentSessionMarker:
    """SubagentStart used to touch a per-session marker file so this hook could skip
    Leader-only reminders in sub-agents. Those reminders are retired, nothing
    reads the markers any more, so the SubagentStart hook no longer writes them."""

    def test_subagent_start_writes_no_marker(self, tmp_path):
        import subprocess
        import sys

        import aiteam.hooks.inject_subagent_context as inject

        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
        env.update(HOME=str(tmp_path), AITEAM_API_URL="http://127.0.0.1:9")
        payload = {"hook_event_name": "SubagentStart", "session_id": "sub-agent-1",
                   "agent_type": "general-purpose", "cwd": str(tmp_path)}
        proc = subprocess.run(
            [sys.executable, inject.__file__], input=json.dumps(payload).encode(),
            capture_output=True, cwd=str(tmp_path), env=env, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / ".claude" / "data" / "ai-team-os" / "subagent_sessions").exists()
        assert not hasattr(workflow_reminder, "_is_subagent_session")


class TestNoWarningNormalFlow:
    """正常流程不应产生多余提醒。"""

    def test_no_warning_normal_flow(self):
        state = {}
        # 普通工具调用不应产生workflow提醒
        for tool in ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]:
            event = {"tool_name": tool, "hook_event_name": "PreToolUse"}
            warnings = _check_workflow_reminders(event, state)
            assert warnings == [], f"Unexpected warning for {tool}: {warnings}"


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
