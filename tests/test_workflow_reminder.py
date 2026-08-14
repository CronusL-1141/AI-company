"""Complete unit tests for aiteam.hooks.workflow_reminder.

Coverage targets:
- _check_agent_team_name: team_name enforcement, readonly bypass, non-Agent pass
- _check_leader_doing_too_much: consecutive call counter, delegation reset
- _check_workflow_reminders: all 14 rules + 4 safety rule groups (S1/S2/S3/S4)

Test philosophy: guilty-until-proven-innocent. Every rule has at least one
positive trigger test and one negative (non-trigger) test. State mutation is
verified explicitly after each call.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
from aiteam.hooks import workflow_reminder as wr
from aiteam.hooks.workflow_reminder import (
    _DELEGATION_TOOLS,
    _LEADER_CONSECUTIVE_THRESHOLD,
    _advance_pipeline_on_completion,
    _bind_subtask_running,
    _check_agent_team_name,
    _check_leader_doing_too_much,
    _check_workflow_reminders,
    _extract_team_identifier,
    _get_running_pipeline_subtask,
    _norm_team_key,
    _post_tool_taskwall_sync,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_urlopen_mock(responses: list[dict]):
    """Return a context-manager mock that yields successive JSON responses.

    Each call to urlopen() consumes one entry from *responses*.
    """
    call_index = {"n": 0}

    def _urlopen(req, timeout=None):
        idx = call_index["n"]
        call_index["n"] += 1
        payload = responses[idx % len(responses)]
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.read = MagicMock(return_value=json.dumps(payload).encode())
        return cm

    return _urlopen


def _teams_response(teams: list[dict]) -> dict:
    return {"data": teams}


def _tasks_response(tasks: list[dict]) -> dict:
    return {"data": tasks}


def _agents_response(agents: list[dict]) -> dict:
    return {"data": agents}


def _git(args: list[str], cwd) -> None:
    subprocess.run(["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _git_out(args: list[str], cwd) -> str:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "master"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "initial"], path)


def _commit_file(repo, name: str, body: str, msg: str) -> str:
    (repo / name).write_text(body)
    _git(["add", name], repo)
    _git(["commit", "-m", msg], repo)
    return _git_out(["rev-parse", "HEAD"], repo)


def _build_long_lived_parent_worktree(
    tmp_path, parent_commits: int = 5, own_commits: int = 1
) -> str:
    """Worktree branched off a LONG-LIVED parent branch, not off master.

    Reproduces the 2026-08-14 false-block measured on a real work repo:
    development sits on a long-lived feature branch ~1090 commits ahead of
    master, so a chore branch cut from it carries ~810 commits that are "not
    landed on master" while at most one of them is its own work. Any criterion
    phrased as "how far is this from the default branch" reports the whole parent
    branch as work at risk and hard-blocks a routine cleanup. The commits are in
    no danger at all: they are reachable from the parent branch ref.

    Layout: master (1 commit) <- feature/long-base (+parent_commits)
            <- worktree-scenario (+1 own commit), worktree clean.
    """
    main_repo = tmp_path / "main"
    _init_repo(main_repo)
    _git(["checkout", "-b", "feature/long-base"], main_repo)
    for i in range(parent_commits):
        _commit_file(main_repo, f"base{i}.txt", f"parent work {i}\n", f"parent commit {i}")
    _git(["checkout", "master"], main_repo)  # master deliberately left behind

    wt_path = tmp_path / "wt"
    _git(
        ["worktree", "add", str(wt_path), "-b", "worktree-scenario", "feature/long-base"],
        main_repo,
    )
    for i in range(own_commits):
        _commit_file(wt_path, f"own{i}.txt", f"own work {i}\n", f"own commit {i}")
    return str(wt_path)


def _build_detached_worktree(tmp_path, kind: str) -> tuple[str, str]:
    """Worktree with a DETACHED HEAD. Returns (worktree_path, head_sha).

    kind:
      - "orphan": a commit made in detached state, reachable from no ref at all.
        Removing this worktree really does orphan it (verified by hand: after
        `git worktree remove` the sha disappears from `git rev-list --all`).
      - "contained": detached at master's tip, so every reachable commit is also
        reachable from refs/heads/master - nothing to lose.
    """
    main_repo = tmp_path / "main"
    _init_repo(main_repo)
    wt_path = tmp_path / "wt"
    _git(["worktree", "add", "--detach", str(wt_path), "master"], main_repo)
    if kind == "orphan":
        head = _commit_file(wt_path, "detached.txt", "only here\n", "detached-state commit")
    elif kind == "contained":
        head = _git_out(["rev-parse", "HEAD"], wt_path)
    else:
        raise ValueError(f"unknown detached kind: {kind}")
    return str(wt_path), head


def _build_worktree_scenario(tmp_path, scenario: str) -> str:
    """Build a real, throwaway git repo + one worktree on branch 'worktree-scenario'.

    Returns the worktree's absolute path. Scenarios:
      - "clean_landed": worktree HEAD == master, nothing to lose (must be removable).
      - "dirty": uncommitted change in the worktree (must hard-block regardless of
        ancestry).
      - "local_unlanded": one commit ahead of master, no upstream configured (must
        hard-block).
      - "pushed_unmerged": one commit ahead of master, pushed to a configured
        upstream (must warn, not hard-block — content is recoverable from the remote).
      - "local_merged_unpushed": the branch was merged into local master with a real
        merge commit (--no-ff, not a fast-forward), but nothing was pushed and
        origin/master is deliberately left stale/behind. Reproduces the 2026-07
        false-block incident (task 1c97d7d9): a batch-push workflow lands work by
        merging locally long before origin catches up, so landed-ness must be judged
        against local master, not origin/master. Must be allowed.
      - "reproduced_all_on_master": the branch's one commit is never merged (no
        ancestor relationship at all), but the identical file diff is independently
        reproduced on master via a separate, differently-shaped commit -- same
        patch-id, different hash. Historical sample from task a1b6a1bf
        (wf_a69e7d46-a66-2). Must be allowed.
      - "reproduced_some_on_master": two commits ahead of master; one is
        independently reproduced on master, the other is genuinely new and never
        reproduced anywhere. Historical sample from task a1b6a1bf
        (wf_a69e7d46-a66-1: 6 commits matched, 1 didn't) - which the pre-2026-08-14
        patch-id criterion hard-blocked. Must be allowed now: the branch ref keeps
        every one of those commits reachable, so a worktree removal cannot lose
        them either way.
    """
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git(["init", "-b", "master"], main_repo)
    _git(["config", "user.email", "test@example.com"], main_repo)
    _git(["config", "user.name", "Test"], main_repo)
    (main_repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], main_repo)
    _git(["commit", "-m", "initial"], main_repo)

    wt_path = tmp_path / "wt"
    _git(["worktree", "add", str(wt_path), "-b", "worktree-scenario"], main_repo)

    if scenario == "clean_landed":
        pass
    elif scenario == "dirty":
        (wt_path / "README.md").write_text("changed but not committed\n")
    elif scenario == "local_unlanded":
        (wt_path / "extra.txt").write_text("local only\n")
        _git(["add", "extra.txt"], wt_path)
        _git(["commit", "-m", "local unlanded work"], wt_path)
    elif scenario == "pushed_unmerged":
        remote_repo = tmp_path / "remote.git"
        _git(["init", "--bare", "-b", "master", str(remote_repo)], tmp_path)
        _git(["remote", "add", "origin", str(remote_repo)], main_repo)
        _git(["push", "origin", "master"], main_repo)
        (wt_path / "extra.txt").write_text("pushed work\n")
        _git(["add", "extra.txt"], wt_path)
        _git(["commit", "-m", "pushed but unmerged"], wt_path)
        _git(["push", "-u", "origin", "worktree-scenario"], wt_path)
    elif scenario == "local_merged_unpushed":
        remote_repo = tmp_path / "remote.git"
        _git(["init", "--bare", "-b", "master", str(remote_repo)], tmp_path)
        _git(["remote", "add", "origin", str(remote_repo)], main_repo)
        _git(["push", "origin", "master"], main_repo)  # origin/master now exists...

        (wt_path / "extra.txt").write_text("work to be merged\n")
        _git(["add", "extra.txt"], wt_path)
        _git(["commit", "-m", "work to be merged"], wt_path)
        # ...and stays stale: master diverges further, locally, after this push.
        (main_repo / "other.txt").write_text("unrelated master-side work\n")
        _git(["add", "other.txt"], main_repo)
        _git(["commit", "-m", "unrelated master work"], main_repo)
        # Real merge commit, not a fast-forward, mirroring the actual incident
        # (merge df446cb landing branch tip aae63ff).
        _git(["merge", "--no-ff", "worktree-scenario", "-m", "merge worktree-scenario"], main_repo)
        # Deliberately never pushed: origin/master is left behind on purpose.
    elif scenario == "reproduced_all_on_master":
        (wt_path / "extra.txt").write_text("reproduced content\n")
        _git(["add", "extra.txt"], wt_path)
        _git(["commit", "-m", "worktree-side commit"], wt_path)
        # Independently reproduce the identical file content on master via a
        # separate, differently-shaped commit (different hash, same patch-id) --
        # mirrors a squash/rebase-elsewhere landing that never makes HEAD a
        # literal ancestor of master.
        (main_repo / "extra.txt").write_text("reproduced content\n")
        _git(["add", "extra.txt"], main_repo)
        _git(["commit", "-m", "master-side reproduction of the same change"], main_repo)
    elif scenario == "reproduced_some_on_master":
        (wt_path / "landed.txt").write_text("this one gets reproduced\n")
        _git(["add", "landed.txt"], wt_path)
        _git(["commit", "-m", "commit A: will be patch-id matched"], wt_path)
        (wt_path / "unlanded.txt").write_text("this one never lands anywhere else\n")
        _git(["add", "unlanded.txt"], wt_path)
        _git(["commit", "-m", "commit B: genuinely new, unmatched"], wt_path)
        # Reproduce only commit A's content on master; commit B stays unmatched.
        (main_repo / "landed.txt").write_text("this one gets reproduced\n")
        _git(["add", "landed.txt"], main_repo)
        _git(["commit", "-m", "master-side reproduction of commit A only"], main_repo)
    else:
        raise ValueError(f"unknown scenario: {scenario}")

    return str(wt_path)


class _GuardExitError(Exception):
    """Stand-in for the process exit an S4 hard block performs."""


def _run_guard(cmd: str, cwd) -> tuple[bool, str, list[str]]:
    """Drive the real hook on one Bash command. Returns (blocked, stderr, warnings).

    sys.exit is replaced by an exception rather than a no-op mock so execution
    stops exactly where the real hook stops - otherwise the guard keeps scanning
    the rest of the command line after a block and the test observes states the
    user never reaches.
    """
    event = {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": cmd}}
    written: list[str] = []

    def _exit(code):
        raise _GuardExitError(code)

    with patch.object(sys, "exit", side_effect=_exit):
        with patch.object(sys.stderr, "write", side_effect=written.append):
            try:
                return False, "", _check_workflow_reminders(event, {})
            except _GuardExitError:
                return True, " ".join(written), []


def _repo_with_worktrees(tmp_path, layout: dict[str, bool]):
    """Repo whose worktrees live under .claude/worktrees/, the real OS layout.

    `layout` maps worktree name -> whether it carries uncommitted work.
    Returns (main repo path, {name: worktree path}).
    """
    main_repo = tmp_path / "main"
    _init_repo(main_repo)
    worktrees = {}
    for name, dirty in layout.items():
        wt = main_repo / ".claude" / "worktrees" / name
        _git(["worktree", "add", str(wt), "-b", f"worktree-{name}"], main_repo)
        if dirty:
            (wt / "scratch.txt").write_text("never committed anywhere\n")
        worktrees[name] = wt
    return main_repo, worktrees


# ===========================================================================
# _check_agent_team_name
# ===========================================================================


class TestCheckAgentTeamName:
    """Tests for _check_agent_team_name."""

    # ------------------------------------------------------------------ #
    # 拦截退役（2026-07-22 缔造者裁定「全面放开+一律自动追踪」，commit 75dcb18）  #
    # 无 team_name 的实施型直派一律放行——SubagentStart 自动收编进会话容器队。      #
    # 本类与 tests/unit/test_agent_check.py 同口径。                              #
    # ------------------------------------------------------------------ #

    def test_impl_keywords_no_team_name_allowed(self):
        """实施关键词（中英）无 team_name → 放行，不 exit 不告警。"""
        prompts = [
            "write the module", "create the database schema",
            "implement the login flow", "fix the bug in auth module",
            "开发用户认证模块", "修复登录接口的500错误",
        ]
        for prompt in prompts:
            event = {"tool_name": "Agent", "tool_input": {"prompt": prompt}}
            with patch.object(sys, "exit") as mock_exit:
                result = _check_agent_team_name(event)
            mock_exit.assert_not_called()
            assert result is None, f"prompt={prompt!r} 应放行"

    # ------------------------------------------------------------------ #
    # Negative: should return None (no exit)                               #
    # ------------------------------------------------------------------ #

    def test_agent_with_team_name_returns_none(self):
        """Agent with team_name present must return None regardless of keywords."""
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "implement login", "team_name": "dev-team"},
        }
        with patch.object(sys, "exit") as mock_exit:
            result = _check_agent_team_name(event)
        mock_exit.assert_not_called()
        assert result is None

    def test_readonly_explore_bypasses_check(self):
        """Agent with 'explore' subagent_type returns None without team_name."""
        event = {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "explore", "prompt": "explore the codebase"},
        }
        with patch.object(sys, "exit") as mock_exit:
            result = _check_agent_team_name(event)
        mock_exit.assert_not_called()
        assert result is None

    def test_readonly_plan_bypasses_check(self):
        """Agent with 'plan' subagent_type returns None."""
        event = {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "plan", "prompt": "plan the architecture"},
        }
        with patch.object(sys, "exit") as mock_exit:
            result = _check_agent_team_name(event)
        mock_exit.assert_not_called()
        assert result is None

    def test_readonly_code_reviewer_bypasses_check(self):
        """Agent with 'code-reviewer' subagent_type returns None."""
        event = {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "code-reviewer"},
        }
        with patch.object(sys, "exit") as mock_exit:
            result = _check_agent_team_name(event)
        mock_exit.assert_not_called()
        assert result is None

    def test_non_agent_tool_returns_none(self):
        """Non-Agent tool names are ignored entirely."""
        for tool in ["Bash", "Read", "Write", "Edit", "TeamCreate", "SendMessage"]:
            event = {"tool_name": tool, "tool_input": {"prompt": "implement stuff"}}
            with patch.object(sys, "exit") as mock_exit:
                result = _check_agent_team_name(event)
            mock_exit.assert_not_called()
            assert result is None, f"Expected None for tool={tool}"

    def test_agent_no_impl_keywords_allowed(self):
        """无实施关键词、无 team_name → 同样放行（全面放开后无差别）。"""
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "please check the logs"},
        }
        with patch.object(sys, "exit") as mock_exit:
            result = _check_agent_team_name(event)
        mock_exit.assert_not_called()
        assert result is None

    def test_empty_tool_input_allowed(self):
        """空 tool_input → 放行（收编由 SubagentStart 兜底，hook 不再拦）。"""
        event = {"tool_name": "Agent", "tool_input": {}}
        with patch.object(sys, "exit") as mock_exit:
            result = _check_agent_team_name(event)
        mock_exit.assert_not_called()
        assert result is None


# ===========================================================================
# _check_leader_doing_too_much
# ===========================================================================


class TestCheckLeaderDoingTooMuch:
    """Tests for _check_leader_doing_too_much."""

    def test_below_threshold_returns_none(self):
        """Consecutive calls up to and including threshold must return None."""
        state: dict = {}
        event = {"tool_name": "Bash"}
        for i in range(_LEADER_CONSECUTIVE_THRESHOLD):
            result = _check_leader_doing_too_much(event, state)
            assert result is None, f"Unexpected warning at call {i + 1}"

    def test_exceeding_threshold_returns_warning(self):
        """Call number threshold+1 must return a warning string."""
        state: dict = {}
        event = {"tool_name": "Read"}
        for _ in range(_LEADER_CONSECUTIVE_THRESHOLD):
            _check_leader_doing_too_much(event, state)
        result = _check_leader_doing_too_much(event, state)
        assert result is not None
        assert "B0.9" in result
        assert str(_LEADER_CONSECUTIVE_THRESHOLD + 1) in result

    def test_warning_contains_consecutive_count(self):
        """Warning message must embed the current consecutive call count."""
        state = {"leader_consecutive_calls": _LEADER_CONSECUTIVE_THRESHOLD}
        event = {"tool_name": "Glob"}
        result = _check_leader_doing_too_much(event, state)
        assert result is not None
        assert str(_LEADER_CONSECUTIVE_THRESHOLD + 1) in result

    def test_agent_delegation_resets_counter(self):
        """Calling Agent resets consecutive counter to 0 and returns None."""
        state = {"leader_consecutive_calls": 7}
        event = {"tool_name": "Agent"}
        result = _check_leader_doing_too_much(event, state)
        assert result is None
        assert state["leader_consecutive_calls"] == 0

    def test_workflow_resets_counter(self):
        """Workflow 编排也是委派动作，重置计数器。

        （原 TeamCreate 版本：该工具于 CC v2.1.219 已不存在，已从
        _DELEGATION_TOOLS 移除，此处改测仍在编制内的 Workflow。）"""
        state = {"leader_consecutive_calls": 7}
        event = {"tool_name": "Workflow"}
        result = _check_leader_doing_too_much(event, state)
        assert result is None
        assert state["leader_consecutive_calls"] == 0

    def test_send_message_resets_counter(self):
        """Calling SendMessage resets counter to 0."""
        state = {"leader_consecutive_calls": 7}
        event = {"tool_name": "SendMessage"}
        result = _check_leader_doing_too_much(event, state)
        assert result is None
        assert state["leader_consecutive_calls"] == 0

    def test_all_delegation_tools_reset(self):
        """All tools in _DELEGATION_TOOLS reset the counter."""
        for tool in _DELEGATION_TOOLS:
            state = {"leader_consecutive_calls": 100}
            event = {"tool_name": tool}
            result = _check_leader_doing_too_much(event, state)
            assert result is None, f"Expected None for delegation tool {tool}"
            assert state["leader_consecutive_calls"] == 0

    def test_reset_then_count_again(self):
        """After delegation reset, counter increments from 0 again."""
        state: dict = {}
        non_deleg = {"tool_name": "Edit"}
        deleg = {"tool_name": "Agent"}

        for _ in range(_LEADER_CONSECUTIVE_THRESHOLD):
            _check_leader_doing_too_much(non_deleg, state)
        _check_leader_doing_too_much(deleg, state)
        assert state["leader_consecutive_calls"] == 0

        # One call after reset should not trigger warning
        result = _check_leader_doing_too_much(non_deleg, state)
        assert result is None
        assert state["leader_consecutive_calls"] == 1

    def test_empty_tool_name_returns_none(self):
        """Empty tool_name must not modify state and must return None."""
        state: dict = {}
        result = _check_leader_doing_too_much({"tool_name": ""}, state)
        assert result is None
        assert "leader_consecutive_calls" not in state

    def test_state_counter_increments_correctly(self):
        """leader_consecutive_calls value in state must increment by 1 each call."""
        state: dict = {}
        event = {"tool_name": "Bash"}
        for expected in range(1, 5):
            _check_leader_doing_too_much(event, state)
            assert state["leader_consecutive_calls"] == expected


# ===========================================================================
# _check_workflow_reminders — Rule 1
# ===========================================================================


class TestRule1TeamCreateTaskWall:
    """Rule 1: TeamCreate → remind about task wall."""

    def test_team_create_warns_task_wall(self):
        """TeamCreate must produce a task-wall reminder."""
        state: dict = {}
        event = {"tool_name": "TeamCreate"}
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        assert any("任务墙" in w for w in warnings)
        assert any("task_create" in w or "task_run" in w for w in warnings)

    def test_non_team_create_no_rule1_warning(self):
        """Bash tool must not produce the Rule 1 reminder."""
        state = {"last_taskwall_view": time.time(), "bottleneck_check_count": 0}
        event = {"tool_name": "Bash", "tool_input": {"command": "echo hello"}}
        warnings = _check_workflow_reminders(event, state)
        assert not any("新团队已创建" in w for w in warnings)


# ===========================================================================
# Rule 2: Agent(team_name) → task wall check + memo reminder
# ===========================================================================


class TestRule2AgentTeamName:
    """Rule 2: Agent with team_name triggers task wall check and memo reminder."""

    def _agent_event(self, team_name: str = "dev-team") -> dict:
        return {
            "tool_name": "Agent",
            "tool_input": {"prompt": "start working", "team_name": team_name},
        }

    def test_no_active_task_produces_taskwall_warning(self):
        """When API returns no running tasks, a task-wall creation reminder appears."""
        state = {"last_memo_reminder": 0}
        api_teams = _teams_response([{"id": "t1", "status": "active", "name": "dev-team"}])
        api_tasks = _tasks_response([])  # No running tasks
        responses = [api_teams, api_tasks]
        urlopen_mock = _make_urlopen_mock(responses)
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            warnings = _check_workflow_reminders(self._agent_event(), state)
        assert any("task_create" in w for w in warnings)

    def test_running_task_exists_no_taskwall_warning(self):
        """When a running task exists, no task-wall creation reminder is produced."""
        state = {"last_memo_reminder": 0}
        api_teams = _teams_response([{"id": "t1", "status": "active"}])
        api_tasks = _tasks_response([{"status": "running", "title": "Build API"}])
        urlopen_mock = _make_urlopen_mock([api_teams, api_tasks])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            warnings = _check_workflow_reminders(self._agent_event(), state)
        assert not any("无进行中任务" in w for w in warnings)

    def test_api_unavailable_does_not_block(self):
        """If API is unreachable, the check must not raise and must not block."""
        state = {"last_memo_reminder": 0}
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            warnings = _check_workflow_reminders(self._agent_event(), state)
        # No crash; memo reminder may still appear
        assert isinstance(warnings, list)

    def test_memo_reminder_appears_when_cooldown_expired(self):
        """Memo reminder appears when last_memo_reminder is 0 (never shown)."""
        state = {"last_memo_reminder": 0}
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(self._agent_event(), state)
        assert any("task_memo_read" in w for w in warnings)

    def test_memo_reminder_suppressed_within_cooldown(self):
        """Memo reminder is suppressed if shown within 5-minute cooldown."""
        state = {"last_memo_reminder": time.time() - 60}  # 1 min ago
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(self._agent_event(), state)
        assert not any("task_memo_read" in w for w in warnings)

    def test_memo_reminder_updates_state_timestamp(self):
        """After showing memo reminder, last_memo_reminder must be updated."""
        before = time.time()
        state = {"last_memo_reminder": 0}
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            _check_workflow_reminders(self._agent_event(), state)
        assert state["last_memo_reminder"] >= before

    def test_agent_without_team_name_no_rule2(self):
        """Agent without team_name in input must not trigger Rule 2 checks."""
        state = {"last_memo_reminder": 0}
        event = {"tool_name": "Agent", "tool_input": {"subagent_type": "explore"}}
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        assert not any("task_memo_read" in w for w in warnings)


# ===========================================================================
# Rule 3: SendMessage(shutdown) → task completion reminder
# ===========================================================================


class TestRule3SendMessageShutdown:
    """Rule 3: SendMessage containing 'shutdown' → remind to mark task done."""

    def test_shutdown_message_produces_completion_reminder(self):
        """'shutdown' in message body triggers task-completion reminder."""
        state: dict = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "dev-agent", "message": "shutdown"},
        }
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        assert any("关闭Agent" in w for w in warnings)
        assert any("task_memo_add" in w for w in warnings)

    def test_shutdown_case_insensitive(self):
        """'SHUTDOWN' in uppercase must also trigger the reminder."""
        state: dict = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "dev-agent", "message": "SHUTDOWN now"},
        }
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        assert any("关闭Agent" in w for w in warnings)

    def test_non_shutdown_send_message_no_rule3(self):
        """Regular SendMessage without 'shutdown' must not trigger Rule 3."""
        state: dict = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "dev-agent", "message": "请继续当前任务"},
        }
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        assert not any("关闭Agent" in w for w in warnings)


# ===========================================================================
# Rule 4: TeamDelete → sync-close ONLY the matching OS team (audit A2 fix)
# ===========================================================================


def _teamdelete_recording_router(teams_resp: dict):
    """urlopen side_effect that records PUT close-calls and serves teams_resp for GET.

    Returns (router, put_urls): put_urls accumulates the URLs of any PUT request,
    letting a test assert exactly which team(s) got closed.
    """
    put_urls: list[str] = []

    def _router(req, timeout=None):
        url = getattr(req, "full_url", str(req))
        method = getattr(req, "method", "GET")
        if method == "PUT":
            put_urls.append(url)
            body: dict = {"success": True}
        else:
            body = teams_resp
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.read = MagicMock(return_value=json.dumps(body).encode())
        return cm

    return _router, put_urls


class TestExtractTeamIdentifier:
    """Unit tests for _extract_team_identifier — the TeamDelete tool_input probe."""

    def test_extracts_team_name(self):
        assert _extract_team_identifier({"team_name": "alpha"}) == "alpha"

    def test_falls_back_to_name_then_ids(self):
        assert _extract_team_identifier({"name": "beta"}) == "beta"
        assert _extract_team_identifier({"team_id": "t-1"}) == "t-1"
        assert _extract_team_identifier({"id": "t-2"}) == "t-2"

    def test_prefers_team_name_over_others(self):
        assert _extract_team_identifier({"id": "t-2", "team_name": "alpha"}) == "alpha"

    def test_strips_whitespace(self):
        assert _extract_team_identifier({"team_name": "  gamma  "}) == "gamma"

    def test_returns_none_for_empty_or_blank(self):
        assert _extract_team_identifier({}) is None
        assert _extract_team_identifier({"team_name": "   "}) is None
        assert _extract_team_identifier({"team_name": 123}) is None
        assert _extract_team_identifier("not-a-dict") is None


class TestNormTeamKey:
    """Unit tests for _norm_team_key — OS↔CC name normalization."""

    def test_lowercases_and_hyphenates(self):
        assert _norm_team_key("Dev Team") == "dev-team"
        assert _norm_team_key("dev-team") == "dev-team"

    def test_handles_none_and_empty(self):
        assert _norm_team_key("") == ""
        assert _norm_team_key(None) == ""  # type: ignore[arg-type]


class TestRule4TeamDelete:
    """Rule 4: TeamDelete must close ONLY the matching OS team, never all active teams.

    Regression guard for audit A2 (2026-07-14, high): the old code closed every
    status=active team on any TeamDelete, corrupting cross-team state under the
    normal multi-session/multi-team parallelism of this repo.
    """

    def _event(self, tool_input: dict) -> dict:
        return {"tool_name": "TeamDelete", "tool_input": tool_input}

    def test_closes_only_matching_team_leaves_others_untouched(self):
        """Two active teams; deleting 'alpha' must PUT only team-A, never team-B."""
        teams_resp = {
            "data": [
                {"id": "team-A", "name": "alpha", "status": "active"},
                {"id": "team-B", "name": "beta", "status": "active"},
            ]
        }
        router, put_urls = _teamdelete_recording_router(teams_resp)
        state: dict = {}
        with patch("urllib.request.urlopen", side_effect=router):
            _check_workflow_reminders(self._event({"team_name": "alpha"}), state)
        assert any("team-A" in u for u in put_urls)
        assert not any("team-B" in u for u in put_urls)

    def test_matches_by_normalized_name(self):
        """'dev-team' identifier matches an OS team named 'Dev Team' (space/case)."""
        teams_resp = {
            "data": [
                {"id": "team-A", "name": "Dev Team", "status": "active"},
                {"id": "team-B", "name": "other", "status": "active"},
            ]
        }
        router, put_urls = _teamdelete_recording_router(teams_resp)
        state: dict = {}
        with patch("urllib.request.urlopen", side_effect=router):
            _check_workflow_reminders(self._event({"team_name": "dev-team"}), state)
        assert any("team-A" in u for u in put_urls)
        assert not any("team-B" in u for u in put_urls)

    def test_matches_by_team_id(self):
        """A raw id identifier closes the team with that id."""
        teams_resp = {
            "data": [
                {"id": "team-A", "name": "alpha", "status": "active"},
                {"id": "team-B", "name": "beta", "status": "active"},
            ]
        }
        router, put_urls = _teamdelete_recording_router(teams_resp)
        state: dict = {}
        with patch("urllib.request.urlopen", side_effect=router):
            _check_workflow_reminders(self._event({"team_id": "team-B"}), state)
        assert any("team-B" in u for u in put_urls)
        assert not any("team-A" in u for u in put_urls)

    def test_no_identifier_is_advisory_only_no_write(self):
        """Unparseable tool_input → advisory reminder, zero PUT calls."""
        teams_resp = {"data": [{"id": "team-A", "name": "alpha", "status": "active"}]}
        router, put_urls = _teamdelete_recording_router(teams_resp)
        state: dict = {}
        with patch("urllib.request.urlopen", side_effect=router):
            warnings = _check_workflow_reminders(self._event({}), state)
        assert put_urls == []
        assert any("无法" in w and "TeamDelete" in w for w in warnings)

    def test_no_match_is_advisory_only_no_write(self):
        """Identifier matching no active team → advisory reminder, zero PUT calls."""
        teams_resp = {"data": [{"id": "team-A", "name": "alpha", "status": "active"}]}
        router, put_urls = _teamdelete_recording_router(teams_resp)
        state: dict = {}
        with patch("urllib.request.urlopen", side_effect=router):
            warnings = _check_workflow_reminders(self._event({"team_name": "ghost"}), state)
        assert put_urls == []
        assert any("ghost" in w and "未匹配" in w for w in warnings)

    def test_does_not_close_inactive_team_with_matching_name(self):
        """A completed team with the target name is not re-closed (only active matched)."""
        teams_resp = {"data": [{"id": "team-A", "name": "alpha", "status": "completed"}]}
        router, put_urls = _teamdelete_recording_router(teams_resp)
        state: dict = {}
        with patch("urllib.request.urlopen", side_effect=router):
            warnings = _check_workflow_reminders(self._event({"team_name": "alpha"}), state)
        assert put_urls == []
        assert any("未匹配" in w for w in warnings)


# ===========================================================================
# Rule 5: TeamCreate with existing active teams → warning
# ===========================================================================


class TestRule5ExistingActiveTeams:
    """Rule 5: Creating a new team when >1 active teams already exist → warning."""

    def test_two_active_teams_produces_warning(self):
        """When API shows 2 active teams after TeamCreate, produce a warning."""
        state: dict = {}
        event = {"tool_name": "TeamCreate"}
        api_resp = _teams_response(
            [
                {"id": "t1", "status": "active", "name": "existing-team"},
                {"id": "t2", "status": "active", "name": "new-team"},
            ]
        )
        urlopen_mock = _make_urlopen_mock([api_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            warnings = _check_workflow_reminders(event, state)
        assert any("已存在活跃团队" in w for w in warnings)

    def test_only_one_active_team_no_rule5_warning(self):
        """When only 1 active team exists (newly created), no Rule 5 warning."""
        state: dict = {}
        event = {"tool_name": "TeamCreate"}
        api_resp = _teams_response([{"id": "t1", "status": "active", "name": "new-team"}])
        urlopen_mock = _make_urlopen_mock([api_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            warnings = _check_workflow_reminders(event, state)
        assert not any("已存在活跃团队" in w for w in warnings)

    def test_api_error_silently_skipped(self):
        """Rule 5 API failure must not raise; just skip the check."""
        state: dict = {}
        event = {"tool_name": "TeamCreate"}
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            warnings = _check_workflow_reminders(event, state)
        # No Rule 5 warning and no exception
        assert not any("已存在活跃团队" in w for w in warnings)


# ===========================================================================
# Rule 7: 15-minute taskwall staleness
# ===========================================================================


class TestRule7TaskwallStaleness:
    """Rule 7: warn if taskwall not viewed for >30 minutes (催办治理：900→1800s)."""

    def test_stale_taskwall_produces_warning(self):
        """After >30 min without a task-wall op, a staleness warning appears."""
        stale_ago = time.time() - 1801
        state = {"last_taskwall_view": stale_ago}
        event = {"tool_name": "Read"}
        warnings = _check_workflow_reminders(event, state)
        assert any("距上次查看任务墙" in w for w in warnings)

    def test_stale_warning_resets_timer(self):
        """After showing staleness warning, last_taskwall_view is reset to now."""
        stale_ago = time.time() - 1801
        state = {"last_taskwall_view": stale_ago}
        before = time.time()
        event = {"tool_name": "Read"}
        _check_workflow_reminders(event, state)
        assert state["last_taskwall_view"] >= before

    def test_within_15_min_no_warning(self):
        """Within 15-minute window, no staleness warning is produced."""
        five_min_ago = time.time() - 300
        state = {"last_taskwall_view": five_min_ago}
        event = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        warnings = _check_workflow_reminders(event, state)
        assert not any("距上次查看任务墙" in w for w in warnings)

    def test_first_call_initializes_timer_no_warning(self):
        """On first call (last_taskwall_view=0), the timer is initialized without warning."""
        state: dict = {}  # last_taskwall_view defaults to 0 via .get
        before = time.time()
        event = {"tool_name": "Edit"}
        warnings = _check_workflow_reminders(event, state)
        assert not any("距上次查看任务墙" in w for w in warnings)
        assert state.get("last_taskwall_view", 0) >= before

    def test_taskwall_view_tool_resets_timer(self):
        """Calling taskwall_view resets last_taskwall_view without generating warning."""
        state = {"last_taskwall_view": 0}
        event = {"tool_name": "taskwall_view"}
        before = time.time()
        warnings = _check_workflow_reminders(event, state)
        assert not any("距上次查看任务墙" in w for w in warnings)
        assert state["last_taskwall_view"] >= before

    def test_mcp_taskwall_view_alias_resets_timer(self):
        """MCP-namespaced taskwall_view alias also resets the timer."""
        state = {"last_taskwall_view": 0}
        event = {"tool_name": "mcp__ai-team-os__taskwall_view"}
        before = time.time()
        _check_workflow_reminders(event, state)
        assert state["last_taskwall_view"] >= before


# ===========================================================================
# Rule 9: SendMessage(completion) → handoff reminder
# ===========================================================================


class TestRule9HandoffReminder:
    """Rule 9: Agent reporting completion triggers handoff/pending-task reminder."""

    def _completion_event(self, extra: str = "") -> dict:
        return {
            "tool_name": "SendMessage",
            "tool_input": {"to": "leader", "message": f"任务已完成，请确认{extra}"},
        }

    def test_completion_message_with_pending_tasks_warns(self):
        """When pending tasks exist after completion report, warn about them."""
        state: dict = {}
        api_teams = _teams_response([{"id": "t1", "status": "active"}])
        api_tasks = _tasks_response(
            [
                {"status": "pending", "title": "Fix tests", "assigned_to": None},
            ]
        )
        urlopen_mock = _make_urlopen_mock([api_teams, api_tasks])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            warnings = _check_workflow_reminders(self._completion_event(), state)
        assert any("待分配任务" in w or "Fix tests" in w for w in warnings)

    def test_completion_message_no_pending_no_rule9_warning(self):
        """No pending tasks → no Rule 9 handoff warning."""
        state: dict = {}
        api_teams = _teams_response([{"id": "t1", "status": "active"}])
        api_tasks = _tasks_response([])
        urlopen_mock = _make_urlopen_mock([api_teams, api_tasks])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            warnings = _check_workflow_reminders(self._completion_event(), state)
        assert not any("待分配任务" in w for w in warnings)

    def test_shutdown_message_not_treated_as_completion(self):
        """Message containing both 'done' and 'shutdown' must not trigger Rule 9."""
        state: dict = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "leader", "message": "done, please shutdown"},
        }
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        # Rule 9 fires only when not shutdown
        assert not any("待分配任务" in w for w in warnings)

    def test_non_completion_send_message_no_rule9(self):
        """Non-completion keywords → no Rule 9 warning."""
        state: dict = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "leader", "message": "正在处理中，请稍等"},
        }
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        assert not any("待分配任务" in w for w in warnings)


# ===========================================================================
# Rule 10: meeting_create → notify participants reminder
# ===========================================================================


class TestRule10MeetingCreate:
    """Rule 10: meeting_create triggers participant notification reminder."""

    def test_meeting_create_produces_reminder(self):
        """meeting_create must produce participant notification reminder."""
        state: dict = {}
        event = {"tool_name": "meeting_create"}
        warnings = _check_workflow_reminders(event, state)
        assert any("通知参与者" in w or "meeting_id" in w for w in warnings)

    def test_mcp_meeting_create_alias_produces_reminder(self):
        """MCP-namespaced meeting_create also produces reminder."""
        state: dict = {}
        event = {"tool_name": "mcp__ai-team-os__meeting_create"}
        warnings = _check_workflow_reminders(event, state)
        assert any("通知参与者" in w or "meeting_id" in w for w in warnings)

    def test_other_tool_no_rule10_warning(self):
        """Non-meeting tools must not produce Rule 10 reminder."""
        state = {"last_taskwall_view": time.time()}
        event = {"tool_name": "Bash", "tool_input": {"command": "echo test"}}
        warnings = _check_workflow_reminders(event, state)
        assert not any("通知参与者" in w for w in warnings)


# ===========================================================================
# Rule 11: meeting_conclude → action items reminder
# ===========================================================================


class TestRule11MeetingConclude:
    """Rule 11: meeting_conclude triggers action items to task wall reminder."""

    def test_meeting_conclude_produces_action_item_reminder(self):
        """meeting_conclude must remind to put action items on the task wall."""
        state: dict = {}
        event = {"tool_name": "meeting_conclude"}
        warnings = _check_workflow_reminders(event, state)
        assert any("行动项" in w or "task_create" in w for w in warnings)

    def test_mcp_meeting_conclude_alias_produces_reminder(self):
        """MCP-namespaced meeting_conclude also triggers the reminder."""
        state: dict = {}
        event = {"tool_name": "mcp__ai-team-os__meeting_conclude"}
        warnings = _check_workflow_reminders(event, state)
        assert any("行动项" in w or "task_create" in w for w in warnings)

    def test_other_tool_no_rule11_warning(self):
        """Other tools must not produce Rule 11 reminder."""
        state = {"last_taskwall_view": time.time()}
        event = {"tool_name": "Read"}
        warnings = _check_workflow_reminders(event, state)
        assert not any("行动项" in w for w in warnings)


# ===========================================================================
# Rule 12: task_status(completed) → QA acceptance reminder
# ===========================================================================


class TestRule12TaskStatusCompleted:
    """Rule 12: marking task completed triggers QA acceptance reminder."""

    def test_task_status_completed_produces_qa_reminder(self):
        """task_status with 'completed' in input produces QA reminder."""
        state: dict = {}
        event = {
            "tool_name": "task_status",
            "tool_input": {"task_id": "t1", "status": "completed"},
        }
        warnings = _check_workflow_reminders(event, state)
        assert any("QA" in w for w in warnings)

    def test_mcp_task_status_completed_produces_qa_reminder(self):
        """MCP-namespaced task_status with 'completed' also triggers reminder."""
        state: dict = {}
        event = {
            "tool_name": "mcp__ai-team-os__task_status",
            "tool_input": {"status": "completed"},
        }
        warnings = _check_workflow_reminders(event, state)
        assert any("QA" in w for w in warnings)

    def test_task_status_in_progress_no_qa_reminder(self):
        """task_status with 'in_progress' must not trigger Rule 12."""
        state: dict = {}
        event = {
            "tool_name": "task_status",
            "tool_input": {"task_id": "t1", "status": "in_progress"},
        }
        warnings = _check_workflow_reminders(event, state)
        assert not any("QA" in w for w in warnings)

    def test_non_task_status_tool_no_rule12(self):
        """Non-task_status tools must not produce Rule 12 warning."""
        state = {"last_taskwall_view": time.time()}
        event = {"tool_name": "Bash", "tool_input": {"command": "echo completed"}}
        warnings = _check_workflow_reminders(event, state)
        assert not any("QA Agent" in w for w in warnings)


# ===========================================================================
# Rule 13: Every 50 calls — bottleneck detection
# ===========================================================================


class TestRule13BottleneckDetection:
    """Rule 13: Every 50 tool calls, check for blocked/all-done situations."""

    def test_non_50th_call_no_bottleneck_check(self):
        """Calls not on the 50th multiple must skip the bottleneck scan."""
        state = {"bottleneck_check_count": 0, "last_taskwall_view": time.time()}
        event = {"tool_name": "Read"}
        with patch("urllib.request.urlopen") as mock_ul:
            for _ in range(49):
                _check_workflow_reminders(event, state)
            # urlopen must not have been called for bottleneck check
            mock_ul.assert_not_called()

    def test_50th_call_triggers_scan(self):
        """The 50th call triggers the bottleneck scan (v1.8.1: project-level task-wall)."""
        state = {"bottleneck_check_count": 49, "last_taskwall_view": time.time()}
        event = {"tool_name": "Read"}
        api_wall = {"stats": {"by_status": {"blocked": 2, "running": 1, "completed": 3}}}
        urlopen_mock = _make_urlopen_mock([api_wall])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            warnings = _check_workflow_reminders(event, state, project_id="p1")
        assert any("阻塞" in w or "blocked" in w.lower() for w in warnings)

    def test_50th_call_without_project_id_skips_scan(self):
        """v1.8.1: no project_id → scan silently skipped (no false '全完成')."""
        state = {"bottleneck_check_count": 49, "last_taskwall_view": time.time()}
        event = {"tool_name": "Read"}
        with patch("urllib.request.urlopen", side_effect=OSError("must not be called")):
            warnings = _check_workflow_reminders(event, state, project_id=None)
        assert not any("任务墙已清空" in w or "阻塞" in w for w in warnings)

    def test_all_tasks_done_produces_empty_wall_notice(self):
        """Project-wide pending+running+blocked all zero → 报事实（不规定必须开会）。"""
        state = {"bottleneck_check_count": 49, "last_taskwall_view": time.time()}
        event = {"tool_name": "Read"}
        api_wall = {"stats": {"by_status": {"completed": 2}}}
        urlopen_mock = _make_urlopen_mock([api_wall])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            warnings = _check_workflow_reminders(event, state, project_id="p1")
        assert any("任务墙已清空" in w for w in warnings)
        assert not any("会议" in w for w in warnings)

    def test_team_empty_but_project_pending_no_false_positive(self):
        """2026-07-12 修复回归锚：项目级仍有 pending 时绝不误报全完成。"""
        state = {"bottleneck_check_count": 49, "last_taskwall_view": time.time()}
        event = {"tool_name": "Read"}
        api_wall = {"stats": {"by_status": {"completed": 18, "pending": 5, "running": 3}}}
        urlopen_mock = _make_urlopen_mock([api_wall])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            warnings = _check_workflow_reminders(event, state, project_id="p1")
        assert not any("任务墙已清空" in w for w in warnings)

    def test_bottleneck_count_increments_every_call(self):
        """bottleneck_check_count must increment on every call."""
        state: dict = {}
        event = {"tool_name": "Read"}
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            for i in range(1, 6):
                _check_workflow_reminders(event, state)
                assert state["bottleneck_check_count"] == i

    def test_api_error_in_bottleneck_check_silently_skipped(self):
        """API errors during Rule 13 scan must not raise exceptions."""
        state = {"bottleneck_check_count": 49, "last_taskwall_view": time.time()}
        event = {"tool_name": "Read"}
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            warnings = _check_workflow_reminders(event, state)
        assert isinstance(warnings, list)


# ===========================================================================
# Rule 14: SendMessage report format validation
# ===========================================================================


class TestRule14ReportFormatValidation:
    """Rule 14: Completion reports must contain standard fields."""

    def test_long_completion_report_missing_fields_warns(self):
        """Long completion report from a member (subagent) session warns on missing fields."""
        state: dict = {}
        long_body = "x" * 101 + " 任务已完成"
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "leader", "message": long_body},
            "session_id": "member-sess",
        }
        # ⑤ 汇报格式提醒改为仅子agent会话触发
        with patch("urllib.request.urlopen", side_effect=Exception("no api")), patch(
            "aiteam.hooks.workflow_reminder._is_subagent_session", return_value=True
        ):
            warnings = _check_workflow_reminders(event, state)
        assert any("完成内容" in w or "修改文件" in w or "测试结果" in w for w in warnings)

    def test_report_with_all_required_fields_no_format_warning(self):
        """Report containing all required fields must not produce format warning."""
        state: dict = {}
        full_report = (
            "任务已完成。\n"
            "完成内容: 实现了登录功能\n"
            "修改文件: src/auth.py\n"
            "测试结果: 全部通过\n"
            "建议任务状态: completed\n"
            "建议memo: 登录功能已上线"
        )
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "leader", "message": full_report},
        }
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        assert not any("缺少标准字段" in w for w in warnings)

    def test_short_completion_message_skips_format_check(self):
        """Short messages (<=100 chars) are exempt from format validation."""
        state: dict = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "leader", "message": "done"},
        }
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        assert not any("缺少标准字段" in w for w in warnings)

    def test_shutdown_report_skips_format_check(self):
        """Shutdown messages are excluded from Rule 14 format check."""
        state: dict = {}
        body = "shutdown " + "x" * 120
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "dev", "message": body},
        }
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            warnings = _check_workflow_reminders(event, state)
        assert not any("缺少标准字段" in w for w in warnings)


# ===========================================================================
# Safety Rule S1: Dangerous Bash commands
# ===========================================================================


class TestSafetyS1DangerousBash:
    """S1: Dangerous Bash command interception."""

    # rm -rf / variants → exit(2) hard block
    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf /",
            "rm -rf ~/",
            "rm -rf ~",
            "rm -r /",
        ],
    )
    def test_rm_rf_root_exits(self, cmd: str):
        """rm -rf targeting root/home must call sys.exit(2)."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write"):
                _check_workflow_reminders(event, state)
        mock_exit.assert_called_once_with(2)

    def test_rm_rf_root_exits_uppercase_r(self):
        """BUG-001 [Fixed]: 'rm -Rf /' with uppercase -R flag is now blocked.

        The S1 safety regex was updated to use [rR] character class to match both cases.
        """
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "rm -Rf /"}}
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write"):
                _check_workflow_reminders(event, state)
        mock_exit.assert_called_once_with(2)

    # rm -rf * → warning (not exit)
    def test_rm_rf_wildcard_produces_warning(self):
        """rm -rf * must produce a safety warning (not exit)."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "rm -rf *"}}
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, state)
        mock_exit.assert_not_called()
        assert any("递归删除" in w or "通配符" in w for w in warnings)

    # DROP TABLE → warning
    def test_drop_table_produces_warning(self):
        """SQL DROP TABLE must produce a safety warning."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "psql -c 'DROP TABLE users'"}}
        warnings = _check_workflow_reminders(event, state)
        assert any("DROP" in w or "数据库" in w for w in warnings)

    def test_drop_database_produces_warning(self):
        """SQL DROP DATABASE must produce a safety warning."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "DROP DATABASE production"}}
        warnings = _check_workflow_reminders(event, state)
        assert any("DROP" in w or "数据库" in w for w in warnings)

    def test_truncate_produces_warning(self):
        """SQL TRUNCATE must produce a safety warning."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "TRUNCATE TABLE orders"}}
        warnings = _check_workflow_reminders(event, state)
        assert any("TRUNCATE" in w or "破坏性" in w for w in warnings)

    # git push --force → warning
    def test_force_push_produces_warning(self):
        """git push --force must produce a safety warning."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "git push origin main --force"}}
        warnings = _check_workflow_reminders(event, state)
        assert any("force push" in w or "force" in w.lower() for w in warnings)

    # chmod 777 → warning
    def test_chmod_777_produces_warning(self):
        """chmod 777 must produce a safety warning."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "chmod 777 /etc/passwd"}}
        warnings = _check_workflow_reminders(event, state)
        assert any("chmod 777" in w or "权限" in w for w in warnings)

    def test_safe_bash_command_no_s1_warning(self):
        """Normal safe Bash commands must not produce S1 warnings."""
        state = {"last_taskwall_view": time.time()}
        event = {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}}
        warnings = _check_workflow_reminders(event, state)
        s1_keywords = ["危险", "rm -rf", "DROP", "force push", "chmod 777"]
        assert not any(any(kw in w for kw in s1_keywords) for w in warnings)


# ===========================================================================
# Safety Rule S3: git add sensitive files
# ===========================================================================


class TestSafetyS3GitAddSensitive:
    """S3: Blocking git add of sensitive files."""

    # .env → exit(2)
    def test_git_add_env_exits(self):
        """git add .env must call sys.exit(2)."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "git add .env"}}
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write"):
                _check_workflow_reminders(event, state)
        mock_exit.assert_called_once_with(2)

    # .pem → exit(2)
    def test_git_add_pem_exits(self):
        """git add *.pem must call sys.exit(2)."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "git add server.pem"}}
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write"):
                _check_workflow_reminders(event, state)
        mock_exit.assert_called_once_with(2)

    # id_rsa → exit(2)
    def test_git_add_id_rsa_exits(self):
        """git add id_rsa (SSH key) must call sys.exit(2)."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "git add ~/.ssh/id_rsa"}}
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write"):
                _check_workflow_reminders(event, state)
        mock_exit.assert_called_once_with(2)

    # .key → exit(2)
    def test_git_add_key_exits(self):
        """git add *.key must call sys.exit(2)."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "git add secret.key"}}
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write"):
                _check_workflow_reminders(event, state)
        mock_exit.assert_called_once_with(2)

    # credentials → warning (not exit)
    def test_git_add_credentials_produces_warning(self):
        """git add credentials must produce a warning but not exit."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "git add credentials.json"}}
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, state)
        mock_exit.assert_not_called()
        assert any("credentials" in w.lower() for w in warnings)

    def test_git_add_safe_file_no_s3_warning(self):
        """git add for a regular source file must not trigger S3."""
        state = {"last_taskwall_view": time.time()}
        event = {"tool_name": "Bash", "tool_input": {"command": "git add src/main.py"}}
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, state)
        mock_exit.assert_not_called()
        assert not any("敏感文件" in w or "id_rsa" in w for w in warnings)

    def test_non_bash_tool_git_add_not_checked(self):
        """S3 check only applies to Bash tool, not Write/Edit."""
        state = {"last_taskwall_view": time.time()}
        event = {"tool_name": "Write", "tool_input": {"file_path": "src/config.py", "content": "x=1"}}
        with patch.object(sys, "exit") as mock_exit:
            _check_workflow_reminders(event, state)
        mock_exit.assert_not_called()


# ===========================================================================
# Safety Rule S4: Worktree teardown protection ("never tear down unlanded work")
# ===========================================================================


class TestSafetyS4WorktreeTeardown:
    """S4: git worktree remove / git branch -D / rm -rf against a worktree dir.

    Each scenario builds a real, throwaway git repo (see _build_worktree_scenario)
    so the git status/merge-base/upstream reads are exercised for real, not mocked.
    """

    def test_clean_landed_worktree_removable(self, tmp_path):
        """Clean worktree whose HEAD is fully merged into master must be allowed,
        with the advisory that the branch itself survives the removal."""
        wt = _build_worktree_scenario(tmp_path, "clean_landed")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert not any("OS BLOCK" in w for w in warnings)
        assert any("worktree-scenario" in w and "不会删除" in w for w in warnings)

    def test_dirty_worktree_hard_blocks(self, tmp_path):
        """Uncommitted/untracked changes must hard-block, regardless of ancestry."""
        wt = _build_worktree_scenario(tmp_path, "dirty")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write") as mock_write:
                _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)
        assert any("未提交" in str(c) for c in mock_write.call_args_list)

    def test_dirty_worktree_hard_blocks_even_with_force(self, tmp_path):
        """--force must not bypass the guard: it is exactly the flag that skips
        git's own dirty-tree check, so the guard treats it as more dangerous,
        not as authorization to proceed."""
        wt = _build_worktree_scenario(tmp_path, "dirty")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove --force "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write"):
                _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)

    def test_local_unmerged_commit_on_attached_branch_is_removable(self, tmp_path):
        """A commit that exists only on this branch, never merged, no upstream:
        removing the WORKTREE cannot lose it, because `git worktree remove` does
        not delete the branch (measured 2026-08-14). Allowed, with the advisory.

        Before 2026-08-14 this hard-blocked - the block that fired on every
        finished-but-unmerged worktree and, on a long-lived parent branch, turned
        one own commit into 810 phantom ones."""
        wt = _build_worktree_scenario(tmp_path, "local_unlanded")
        main_repo = tmp_path / "main"
        head = _git_out(["rev-parse", "HEAD"], wt)
        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert any("worktree-scenario" in w and "不会删除" in w for w in warnings)
        # The premise, asserted rather than assumed: the removal really is lossless.
        _git(["worktree", "remove", wt], main_repo)
        assert _git_out(["rev-list", head, "--not", "--branches", "--remotes", "--tags"], main_repo) == ""

    def test_pushed_unmerged_commit_is_removable(self, tmp_path):
        """A commit pushed to a configured upstream but not yet merged: doubly
        safe (branch ref + remote-tracking ref), so no block."""
        wt = _build_worktree_scenario(tmp_path, "pushed_unmerged")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert not any("OS BLOCK" in w for w in warnings)

    def test_locally_merged_but_unpushed_worktree_is_removable(self, tmp_path):
        """Regression for task 1c97d7d9: a branch merged into local master with a
        real merge commit must be treated as landed and allowed, even when
        origin/master is deliberately stale/behind (batch-push workflow — push is
        done by the user later, not on every local merge). Landed-ness must be
        judged against the local main branch, not a possibly-lagging origin ref."""
        wt = _build_worktree_scenario(tmp_path, "local_merged_unpushed")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert not any("OS BLOCK" in w for w in warnings)

    def test_content_reproduced_on_master_worktree_removable(self, tmp_path):
        """Historical sample from task a1b6a1bf (wf_a69e7d46-a66-2): a branch never
        merged into master whose content was independently reproduced elsewhere.
        Allowed - now for the simpler reason that its branch ref still holds it."""
        wt = _build_worktree_scenario(tmp_path, "reproduced_all_on_master")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert not any("OS BLOCK" in w for w in warnings)

    def test_partially_reproduced_content_no_longer_blocks(self, tmp_path):
        """Historical sample from task a1b6a1bf (wf_a69e7d46-a66-1): only SOME
        commits ahead of master are patch-id equivalent. The old criterion called
        that "mixed, therefore unsafe" and hard-blocked; under reachability it is
        plainly safe - both commits stay on refs/heads/worktree-scenario after the
        worktree is gone. Kept as a regression pin for the criterion change."""
        wt = _build_worktree_scenario(tmp_path, "reproduced_some_on_master")
        main_repo = tmp_path / "main"
        head = _git_out(["rev-parse", "HEAD"], wt)
        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert not any("OS BLOCK" in w for w in warnings)
        _git(["worktree", "remove", wt], main_repo)
        assert _git_out(["rev-list", head, "--not", "--branches", "--remotes", "--tags"], main_repo) == ""

    def test_rm_rf_dirty_worktree_dir_hard_blocks_same_as_worktree_remove(self, tmp_path):
        """rm -rf on a .claude/worktrees/ path bypasses git's own safety net
        entirely — uncommitted work there has no ref to fall back on, so it must
        be caught by the same assessment as `git worktree remove`."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        wt_dir = main_repo / ".claude" / "worktrees" / "scenario"
        _git(["worktree", "add", str(wt_dir), "-b", "worktree-scenario"], main_repo)
        _commit_file(wt_dir, "extra.txt", "committed work\n", "committed work")
        (wt_dir / "in-flight.txt").write_text("never committed anywhere\n")

        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": f'rm -rf "{wt_dir}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write") as mock_write:
                _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)
        blocked = " ".join(str(c) for c in mock_write.call_args_list)
        assert "rm -rf" in blocked
        assert "未提交" in blocked

    def test_rm_rf_clean_worktree_dir_allowed(self, tmp_path):
        """Same rm -rf, clean tree, committed work on an attached branch: the
        branch ref keeps every commit, so there is nothing to hard-block."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        wt_dir = main_repo / ".claude" / "worktrees" / "scenario"
        _git(["worktree", "add", str(wt_dir), "-b", "worktree-scenario"], main_repo)
        _commit_file(wt_dir, "extra.txt", "committed work\n", "committed work")

        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": f'rm -rf "{wt_dir}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert any("worktree-scenario" in w and "不会删除" in w for w in warnings)

    def test_branch_dash_capital_d_hard_blocks_unmerged_worktree_branch(self, tmp_path):
        """git branch -D on a worktree-prefixed branch with unmerged, unpushed
        commits must hard-block, same as the worktree-remove path."""
        wt = _build_worktree_scenario(tmp_path, "local_unlanded")
        main_repo = tmp_path / "main"
        # Detach the worktree branch's checkout first — git refuses to delete a
        # branch checked out in another worktree regardless of -D, so remove the
        # worktree registration (not the branch) to exercise the branch-D path
        # against an orphaned-but-unmerged branch, same as a post-removal cleanup.
        _git(["worktree", "remove", "--force", wt], main_repo)

        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": "git branch -D worktree-scenario"},
        }
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write") as mock_write:
                _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)
        assert any("强删分支" in str(c) for c in mock_write.call_args_list)

    def test_branch_dash_capital_d_allows_landed_branch(self, tmp_path):
        """git branch -D on a branch that is fully merged (or never diverged)
        must not be blocked."""
        wt = _build_worktree_scenario(tmp_path, "clean_landed")
        main_repo = tmp_path / "main"
        _git(["worktree", "remove", wt], main_repo)

        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": "git branch -D worktree-scenario"},
        }
        with patch.object(sys, "exit") as mock_exit:
            _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()

    def test_worktree_remove_nonexistent_path_not_crash_no_block(self, tmp_path):
        """A path that doesn't resolve to a real worktree must be skipped
        silently (git itself will report the real error) — the guard must never
        crash or falsely block on an assessment it could not perform."""
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path),
            "tool_input": {"command": 'git worktree remove "/no/such/path/at/all"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()

    def test_unrelated_bash_command_not_affected_by_s4(self):
        """S4 must not fire (or slow anything down) for ordinary Bash commands."""
        state = {"last_taskwall_view": time.time()}
        event = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, state)
        mock_exit.assert_not_called()
        assert not any("worktree" in w.lower() for w in warnings)


# ===========================================================================
# Safety Rule S4 (2026-08-14 re-aim): criterion is git reachability, not
# "has this landed on the default branch"
# ===========================================================================


class TestSafetyS4ReachabilityCriterion:
    """The two defects the 2026-08-14 rework fixes, each as a real git repo.

    (1) Wrong target: `git worktree remove` does NOT delete branches (measured:
        after removing a worktree whose HEAD is attached, the branch ref is still
        there and `git rev-list <sha> --not --branches --remotes --tags` is empty).
        Only a DETACHED worktree can orphan commits. Hard-blocking committed work
        on an attached branch protects nothing and blocks routine cleanup.
    (2) Wrong baseline: comparing against origin/HEAD's master/main explodes on
        long-lived parent branches (real sample: 810 "not equivalent" commits vs 1
        that is actually the branch's own work).
    """

    def test_long_lived_parent_branch_worktree_is_removable(self, tmp_path):
        """Defect 2: a clean worktree cut from a long-lived parent branch must be
        removable. Its commits stay reachable from refs/heads/worktree-scenario
        (and from the parent branch), so nothing can be lost by removing the
        worktree - the only honest output is an advisory that the branch stays."""
        wt = _build_long_lived_parent_worktree(tmp_path, parent_commits=5)
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write") as mock_write:
                warnings = _check_workflow_reminders(event, {})
        blocked = " ".join(str(c) for c in mock_write.call_args_list)
        mock_exit.assert_not_called()
        assert "OS BLOCK" not in blocked
        assert any("worktree-scenario" in w and "不会删除" in w for w in warnings)

    def test_detached_head_orphan_commit_still_hard_blocks(self, tmp_path):
        """Defect 1's flip side: detached HEAD is the case that really orphans
        commits, so it must stay hard-blocked - and the message must name the
        orphan commits, since "unreachable from every ref" is the actual reason,
        not "not merged into master"."""
        wt, head = _build_detached_worktree(tmp_path, "orphan")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write") as mock_write:
                _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)
        blocked = " ".join(str(c) for c in mock_write.call_args_list)
        assert "游离" in blocked
        assert head[:7] in blocked

    def test_detached_head_already_covered_by_a_ref_is_removable(self, tmp_path):
        """Detached, but every reachable commit is also reachable from master:
        nothing is orphaned, so no block."""
        wt, _head = _build_detached_worktree(tmp_path, "contained")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert not any("OS BLOCK" in w for w in warnings)

    def test_detached_head_with_dirty_tree_still_hard_blocks(self, tmp_path):
        """Dirty beats everything: uncommitted work has no ref at all."""
        wt, _head = _build_detached_worktree(tmp_path, "contained")
        (pathlib.Path(wt) / "scratch.txt").write_text("in flight\n")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove --force "{wt}"'},
        }
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write") as mock_write:
                _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)
        assert any("未提交" in str(c) for c in mock_write.call_args_list)

    def test_branch_d_covered_by_long_lived_parent_is_allowed(self, tmp_path):
        """Defect 2 on the `git branch -D` path: a branch cut from a long-lived
        parent, carrying no commits of its own, is 5 commits "unlanded" relative to
        master and 0 relative to reality. Deleting the ref loses nothing."""
        wt = _build_long_lived_parent_worktree(tmp_path, parent_commits=5, own_commits=0)
        main_repo = tmp_path / "main"
        _git(["worktree", "remove", wt], main_repo)

        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": "git branch -D worktree-scenario"},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert not any("OS BLOCK" in w for w in warnings)

    def test_branch_d_covered_by_remote_ref_is_allowed(self, tmp_path):
        """A branch whose commits are on a remote-tracking ref survives its own
        deletion: refs/remotes/origin/... still reaches them."""
        wt = _build_worktree_scenario(tmp_path, "pushed_unmerged")
        main_repo = tmp_path / "main"
        _git(["worktree", "remove", wt], main_repo)

        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": "git branch -D worktree-scenario"},
        }
        with patch.object(sys, "exit") as mock_exit:
            warnings = _check_workflow_reminders(event, {})
        mock_exit.assert_not_called()
        assert not any("OS BLOCK" in w for w in warnings)

    def test_branch_d_true_orphan_hard_blocks_and_names_the_commit(self, tmp_path):
        """The self-exclusion trap: `git rev-list <b> --not --branches --remotes
        --tags` counts <b> itself and is therefore empty for EVERY live branch -
        a blanket false allow. This case only blocks if refs/heads/<b> is filtered
        out of the exclusion list, so it pins that behaviour."""
        wt = _build_worktree_scenario(tmp_path, "local_unlanded")
        main_repo = tmp_path / "main"
        head = _git_out(["rev-parse", "HEAD"], wt)
        _git(["worktree", "remove", "--force", wt], main_repo)

        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": "git branch -D worktree-scenario"},
        }
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write") as mock_write:
                _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)
        blocked = " ".join(str(c) for c in mock_write.call_args_list)
        assert "强删分支" in blocked
        assert head[:7] in blocked

    def test_branch_d_checks_every_operand_not_just_the_first(self, tmp_path):
        """`git branch -D a b` deletes both. Examining only the first operand
        would wave the second one through unexamined - false ALLOW. Here the
        first branch is safely contained in master and the second is the orphan."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        _git(["branch", "worktree-safe"], main_repo)  # points at master, loses nothing
        _git(["checkout", "-q", "-b", "worktree-risky"], main_repo)
        head = _commit_file(main_repo, "only-here.txt", "orphan work\n", "orphan work")
        _git(["checkout", "-q", "master"], main_repo)

        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": "git branch -D worktree-safe worktree-risky"},
        }
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write") as mock_write:
                _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)
        blocked = " ".join(str(c) for c in mock_write.call_args_list)
        assert "worktree-risky" in blocked
        assert head[:7] in blocked

    def test_orphan_probe_failure_blocks_worktree_removal(self, tmp_path):
        """Fail-closed: an undeterminable reachability probe on a detached HEAD
        must block. A false ALLOW loses work silently; a false BLOCK costs one
        --force."""
        wt, _head = _build_detached_worktree(tmp_path, "contained")
        event = {
            "tool_name": "Bash",
            "cwd": str(tmp_path / "main"),
            "tool_input": {"command": f'git worktree remove "{wt}"'},
        }
        with patch.object(wr, "_orphan_commits", return_value=(False, [])):
            with patch.object(sys, "exit") as mock_exit:
                with patch.object(sys.stderr, "write") as mock_write:
                    _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)
        assert any("探测失败" in str(c) for c in mock_write.call_args_list)

    def test_orphan_probe_failure_blocks_branch_deletion(self, tmp_path):
        """Same fail-closed rule on the branch -D path."""
        wt = _build_worktree_scenario(tmp_path, "clean_landed")
        main_repo = tmp_path / "main"
        _git(["worktree", "remove", wt], main_repo)
        event = {
            "tool_name": "Bash",
            "cwd": str(main_repo),
            "tool_input": {"command": "git branch -D worktree-scenario"},
        }
        with patch.object(wr, "_orphan_commits", return_value=(False, [])):
            with patch.object(sys, "exit") as mock_exit:
                with patch.object(sys.stderr, "write") as mock_write:
                    _check_workflow_reminders(event, {})
        mock_exit.assert_called_once_with(2)
        assert any("探测失败" in str(c) for c in mock_write.call_args_list)

    def test_orphan_commits_reports_undetermined_on_non_repo(self, tmp_path):
        """The probe itself must say "undetermined" rather than "nothing found"
        when git cannot answer - that distinction is what makes fail-closed work."""
        determined, orphans = wr._orphan_commits(str(tmp_path), "HEAD")
        assert determined is False
        assert orphans == []

    def test_orphan_probe_survives_hundreds_of_refs(self, tmp_path):
        """Exclusions go through `rev-list --stdin`: a repo with hundreds of refs
        must not blow the argv limit (the reason the list is not spliced into the
        command line)."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        head = _git_out(["rev-parse", "HEAD"], main_repo)
        for i in range(400):
            _git(["tag", f"very-long-tag-name-for-argv-pressure-{i:04d}"], main_repo)
        determined, orphans = wr._orphan_commits(str(main_repo), head)
        assert determined is True
        assert orphans == []


# ===========================================================================
# Safety Rule S4 (2026-08-14 round 2): the adversarial review of the re-aim
#
# Every case below was first reproduced on a real repo as a false ALLOW that
# really destroyed work (the reviewers ran the allowed command and then verified
# the loss with rev-list / the filesystem). They are grouped here rather than
# merged into the classes above so the attack surface stays legible: command
# recognition, mutual alibi, probe failure, and "git cannot get it back" content
# that lives outside commits.
# ===========================================================================


class TestSafetyS4AdversarialRound2:
    """Round-2 hardening. Each test names the false ALLOW it pins shut."""

    # -- command recognition -------------------------------------------------

    @pytest.mark.parametrize(
        "flags",
        ["--force", "-f", "-ff", "-f -f", "--force --force", "-f --", "--"],
    )
    def test_dirty_worktree_blocks_for_every_force_spelling(self, tmp_path, flags):
        """`-ff` and `-f --` used to skip the whole S4 evaluation: the flag filter
        removed only the literal '--force'/'-f', so '-ff' and '--' were taken for
        the path operand and no path resolved. `-ff` is not exotic - it is what
        git itself tells you to use on a locked worktree, which is exactly how CC
        creates review worktrees."""
        wt = _build_worktree_scenario(tmp_path, "dirty")
        blocked, err, _ = _run_guard(f'git worktree remove {flags} "{wt}"', tmp_path / "main")
        assert blocked, f"{flags} bypassed the guard"
        assert "未提交" in err

    @pytest.mark.parametrize(
        "cmd",
        [
            "git worktree remove",
            'git worktree remove --force "$WT"',
            "git worktree list --porcelain | xargs git worktree remove",
        ],
    )
    def test_unresolvable_worktree_target_blocks(self, tmp_path, cmd):
        """A teardown verb whose target cannot be pinned down is the one case
        where silence is indistinguishable from safety - so it blocks."""
        main_repo, _ = _repo_with_worktrees(tmp_path, {"wf_a": False})
        blocked, err, _ = _run_guard(cmd, main_repo)
        assert blocked
        assert "解析不出" in err

    @pytest.mark.parametrize(
        "cmd",
        [
            "git branch --delete --force worktree-risky",
            "git branch --force --delete worktree-risky",
            "git branch -D -f worktree-risky",
            "git branch -f -D worktree-risky",
            "git branch -q -D worktree-risky",
            "git branch -D worktree-risky # cleanup",
            "git branch \\\n    -D worktree-risky",
            "git branch -D -- worktree-risky",
            "git update-ref -d refs/heads/worktree-risky",
            "(git branch -D worktree-risky)",
        ],
    )
    def test_every_ref_deletion_spelling_is_recognized(self, tmp_path, cmd):
        """One regex per spelling leaked: --delete --force, -D -f, -q -D, line
        continuations and update-ref all deleted the ref while the guard stayed
        quiet. Recognition is now on tokens, so a flag is a flag however spelled."""
        repo = self._repo_with_orphan_branch(tmp_path)
        blocked, err, _ = _run_guard(cmd, repo)
        assert blocked, f"{cmd!r} bypassed the guard"
        assert "worktree-risky" in err

    @pytest.mark.parametrize(
        "cmd",
        [
            "git branch --list 'worktree-*' | xargs git branch -D",
            "for b in $(git branch --list 'worktree-*'); do git branch -D $b; done",
            'git branch -D "$BRANCH"',
        ],
    )
    def test_batch_ref_deletion_without_literal_operands_blocks(self, tmp_path, cmd):
        """Operands that only exist at runtime (xargs, $var, command substitution)
        cannot be probed, so they take the conservative branch and ask for an
        explicit branch name."""
        repo = self._repo_with_orphan_branch(tmp_path)
        blocked, err, _ = _run_guard(cmd, repo)
        assert blocked, f"{cmd!r} bypassed the guard"
        assert "解析不出" in err

    def test_ref_deletion_is_probed_in_the_repo_it_targets(self, tmp_path):
        """`git -C <repo>` and `cd <repo> &&` are how an agent deletes a branch
        without changing the session's cwd. Probing the event cwd instead found
        no such branch, read that as "nothing to lose" and allowed it."""
        repo = self._repo_with_orphan_branch(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        for cmd in (
            f'git -C "{repo}" branch -D worktree-risky',
            f'cd "{repo}" && git branch -D worktree-risky',
            f'git --git-dir="{repo}/.git" branch -D worktree-risky',
        ):
            blocked, err, _ = _run_guard(cmd, elsewhere)
            assert blocked, f"{cmd!r} bypassed the guard"
            assert "worktree-risky" in err

    def test_quoted_command_text_is_not_a_deletion(self, tmp_path):
        """The flip side of wider recognition: a branch name inside a commit
        message is text, not a command. Tokenizing (rather than regexing the raw
        line) is what keeps this from becoming a false block."""
        repo = self._repo_with_orphan_branch(tmp_path)
        blocked, _, _ = _run_guard(
            'git commit --allow-empty -m "cleanup: git branch -D worktree-risky"', repo
        )
        assert not blocked

    # -- scope: the branch is the last thing holding the commits -------------

    def test_worktree_removal_then_branch_delete_blocks(self, tmp_path):
        """The compound attack the re-aim opened up: removing the worktree is
        allowed (the branch survives), and the follow-up `git branch -D` used to
        be out of scope for anything not named worktree-*. Real work branches are
        named chore/…, so the two allowed halves added up to a silent loss."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        wt = tmp_path / "wt"
        _git(["worktree", "add", str(wt), "-b", "chore/cleanup"], main_repo)
        head = _commit_file(wt, "only-here.txt", "sole copy\n", "sole copy")

        blocked, err, _ = _run_guard(
            f'git worktree remove "{wt}" && git branch -D chore/cleanup', main_repo
        )
        assert blocked
        assert "chore/cleanup" in err
        assert head[:7] in err

    def test_advisory_says_when_the_branch_is_the_only_reference(self, tmp_path):
        """The advisory is also an instruction. Telling the operator "the branch
        stays, delete it separately to finish the cleanup" is exactly wrong when
        that branch is the last ref reaching the commits."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        wt = tmp_path / "wt"
        _git(["worktree", "add", str(wt), "-b", "chore/cleanup"], main_repo)
        head = _commit_file(wt, "only-here.txt", "sole copy\n", "sole copy")

        blocked, _, warnings = _run_guard(f'git worktree remove "{wt}"', main_repo)
        assert not blocked
        advisory = " ".join(warnings)
        assert "唯一引用" in advisory
        assert head[:7] in advisory

    def test_branch_delete_pair_cannot_alibi_each_other(self, tmp_path):
        """Two branches on the same tip vouched for each other: each probe
        excluded only its own ref, found the other still reaching the commits,
        and allowed both deletions. The exclusion set is now the union of every
        ref the command line destroys."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        _git(["checkout", "-q", "-b", "worktree-wf_x"], main_repo)
        head = _commit_file(main_repo, "only-here.txt", "sole copy\n", "sole copy")
        _git(["branch", "worktree-wf_x-backup"], main_repo)
        _git(["checkout", "-q", "master"], main_repo)

        # Deleting one is genuinely safe - the backup still holds the commit.
        blocked, _, _ = _run_guard("git branch -D worktree-wf_x", main_repo)
        assert not blocked

        for cmd in (
            "git branch -D worktree-wf_x worktree-wf_x-backup",
            "git branch -D worktree-wf_x && git branch -D worktree-wf_x-backup",
            "git branch -D worktree-wf_x; git branch -D worktree-wf_x-backup",
        ):
            blocked, err, _ = _run_guard(cmd, main_repo)
            assert blocked, f"{cmd!r} bypassed the guard"
            assert head[:7] in err

        # The premise, asserted rather than assumed: both deletions really do
        # leave the commit unreachable from every ref.
        _git(["branch", "-D", "worktree-wf_x"], main_repo)
        _git(["branch", "-D", "worktree-wf_x-backup"], main_repo)
        assert (
            _git_out(["rev-list", head, "--not", "--branches", "--remotes", "--tags"], main_repo)
            != ""
        )

    def test_refname_exclusion_folds_case_and_unicode(self, tmp_path):
        """On a case-insensitive / normalizing filesystem the typed spelling and
        the stored spelling are the same ref, but string equality says otherwise -
        so the doomed ref stayed in the exclusion list, the orphan set came back
        empty for every branch, and the guard waved everything through."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        _git(["checkout", "-q", "-b", "worktree-Wf_Case"], main_repo)
        head = _commit_file(main_repo, "only-here.txt", "sole copy\n", "sole copy")
        _git(["checkout", "-q", "master"], main_repo)

        determined, orphans = wr._orphan_commits(
            str(main_repo),
            "refs/heads/worktree-Wf_Case",
            exclude_refs=("refs/heads/worktree-wf_case",),
        )
        assert determined is True
        assert orphans and orphans[0] == head[: len(orphans[0])]

        # End to end, only where the filesystem actually folds refnames.
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/worktree-wf_case"],
            cwd=str(main_repo),
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            pytest.skip("case-sensitive ref storage: the typed spelling is a different ref")
        blocked, err, _ = _run_guard("git branch -D worktree-wf_case", main_repo)
        assert blocked
        assert head[:7] in err

    # -- multi-target commands ----------------------------------------------

    def test_second_teardown_target_is_assessed_too(self, tmp_path):
        """Only the first operand used to be examined, which made protection a
        function of operand order: dirty-first blocked, dirty-second passed."""
        main_repo, wts = _repo_with_worktrees(tmp_path, {"wf_clean": False, "wf_dirty": True})
        clean, dirty = wts["wf_clean"], wts["wf_dirty"]
        for cmd in (
            f'git worktree remove --force "{clean}" && git worktree remove --force "{dirty}"',
            f'rm -rf "{clean}" "{dirty}"',
        ):
            blocked, err, _ = _run_guard(cmd, main_repo)
            assert blocked, f"{cmd!r} bypassed the guard"
            assert "未提交" in err

    @pytest.mark.parametrize(
        "target",
        [".claude/worktrees", ".claude/worktrees/", ".claude/worktrees/*", "."],
    )
    def test_rm_rf_of_the_worktrees_container_blocks(self, tmp_path, target):
        """The recognizer required a path segment after .claude/worktrees/, so
        deleting the container itself - the exact command the CHANGELOG claims is
        covered - took every worktree with it, unexamined."""
        main_repo, _ = _repo_with_worktrees(tmp_path, {"wf_a": False, "wf_b": True})
        blocked, err, _ = _run_guard(f"rm -rf {target}", main_repo)
        assert blocked, f"rm -rf {target} bypassed the guard"
        assert "未提交" in err

    def test_rm_rf_with_a_variable_target_blocks(self, tmp_path):
        """`rm -rf "$WT_DIR"` cannot be resolved here, and an unresolvable target
        under the worktrees directory is precisely the dangerous case."""
        main_repo, _ = _repo_with_worktrees(tmp_path, {"wf_a": True})
        blocked, err, _ = _run_guard('rm -rf "$HOME/.claude/worktrees/wf_a"', main_repo)
        assert blocked
        assert "解析不出" in err

    @pytest.mark.parametrize(
        "cmd",
        [
            "find .claude/worktrees -mindepth 1 -maxdepth 1 -type d | xargs rm -rf",
            "find .claude/worktrees -mindepth 1 -maxdepth 1 -exec rm -rf {} +",
        ],
    )
    def test_rm_rf_fed_by_a_pipeline_blocks(self, tmp_path, cmd):
        """The target never appears as a token: it arrives through the pipe or as
        find's {} placeholder. Skipping what cannot be parsed is the same silent
        pass-through the flag filter used to produce."""
        main_repo, _ = _repo_with_worktrees(tmp_path, {"wf_a": True})
        blocked, err, _ = _run_guard(cmd, main_repo)
        assert blocked
        assert "解析不出" in err

    def test_rm_rf_of_a_worktree_outside_the_managed_directory_blocks(self, tmp_path):
        """Worktrees are not always parked under .claude/worktrees - the repo that
        motivated this guard keeps one in /tmp. `rm -rf` there destroys
        uncommitted work just as thoroughly, so recognition follows the linked
        worktree's own signature (.git is a file, not a directory)."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        wt = tmp_path / "elsewhere" / "push1"
        _git(["worktree", "add", str(wt), "-b", "chore/snapshot"], main_repo)
        (wt / "scratch.txt").write_text("never committed anywhere\n")
        blocked, err, _ = _run_guard(f'rm -rf "{wt}"', main_repo)
        assert blocked
        assert "未提交" in err

    def test_unrelated_rm_alongside_a_worktree_mention_is_not_blocked(self, tmp_path):
        """The counterweight: an explicit, unrelated target is parsed and found
        harmless even when the same line mentions the worktrees directory."""
        main_repo, _ = _repo_with_worktrees(tmp_path, {"wf_a": True})
        (main_repo / "dist").mkdir()
        blocked, _, _ = _run_guard("ls .claude/worktrees && rm -rf dist", main_repo)
        assert not blocked

    # -- probe failure must never read as "nothing found" -------------------

    def test_uninspectable_worktree_directory_blocks_rm_rf(self, tmp_path):
        """After `git worktree prune` the admin entry is gone, `git status` fails
        and the old code returned "nothing to say" - but the files, including
        uncommitted ones, are still on disk and rm -rf does not complain. The
        "git will report the right error" argument only holds for git's own
        commands."""
        main_repo, wts = _repo_with_worktrees(tmp_path, {"wf_stale": True})
        shutil.rmtree(main_repo / ".git" / "worktrees" / "wf_stale")
        blocked, err, _ = _run_guard(f'rm -rf "{wts["wf_stale"]}"', main_repo)
        assert blocked
        assert "无法检查" in err

    def test_plain_directory_without_git_state_is_not_blocked(self, tmp_path):
        """The one place silence is still right: nothing git-related to lose."""
        base = tmp_path / "notarepo"
        leftover = base / ".claude" / "worktrees" / "leftover"
        leftover.mkdir(parents=True)
        (leftover / "note.txt").write_text("just a directory\n")
        blocked, _, _ = _run_guard(f'rm -rf "{leftover}"', base)
        assert not blocked

    def test_empty_leftover_directory_is_not_blocked(self, tmp_path):
        """The status probe asks about the target, not about the repo that
        happens to contain it: an empty leftover directory must not inherit the
        main checkout's unrelated edits and become unremovable."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        leftover = main_repo / ".claude" / "worktrees" / "leftover"
        leftover.mkdir(parents=True)
        (main_repo / "unrelated-edit.txt").write_text("main checkout is dirty\n")
        blocked, _, _ = _run_guard(f'rm -rf "{leftover}"', main_repo)
        assert not blocked

    def test_untracked_content_in_the_target_blocks(self, tmp_path):
        """...but content that only exists inside the target still blocks, even
        when the target is not a registered worktree."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        leftover = main_repo / ".claude" / "worktrees" / "leftover"
        leftover.mkdir(parents=True)
        (leftover / "note.txt").write_text("only copy of this note\n")
        blocked, err, _ = _run_guard(f'rm -rf "{leftover}"', main_repo)
        assert blocked
        assert "未提交" in err

    def test_status_probe_failure_blocks_teardown(self, tmp_path):
        """A worktree whose status read times out is not a clean worktree."""
        wt = _build_worktree_scenario(tmp_path, "clean_landed")
        real = wr._run_git_readonly

        def fake(args, cwd, **kwargs):
            if "status" in args:
                return wr._GIT_UNAVAILABLE, ""
            return real(args, cwd, **kwargs)

        with patch.object(wr, "_run_git_readonly", side_effect=fake):
            blocked, err, _ = _run_guard(f'git worktree remove "{wt}"', tmp_path / "main")
        assert blocked
        assert "探测失败" in err

    def test_unavailable_git_blocks_ref_deletion(self, tmp_path):
        """`_run_git_readonly` used to collapse "git is not on PATH" and "this is
        not a repository" into the same value, and the branch path read that as
        permission to continue."""
        repo = self._repo_with_orphan_branch(tmp_path)
        with patch.object(wr, "_run_git_readonly", return_value=(wr._GIT_UNAVAILABLE, "")):
            blocked, err, _ = _run_guard("git branch -D worktree-risky", repo)
        assert blocked
        assert "探测" in err

    def test_broken_object_store_makes_the_probe_undetermined(self, tmp_path):
        """The only real false-ALLOW channel left in _orphan_commits is rev-list
        exiting non-zero being read as "no orphans". Reachable for real: an
        unreadable parent object makes for-each-ref and rev-parse succeed while
        rev-list fails (measured: fatal: bad object, rc 128)."""
        repo = self._repo_with_orphan_branch(tmp_path)
        parent = _git_out(["rev-parse", "refs/heads/worktree-risky^"], repo)
        obj = pathlib.Path(repo) / ".git" / "objects" / parent[:2] / parent[2:]
        os.chmod(obj, 0o600)
        obj.unlink()

        determined, orphans = wr._orphan_commits(
            str(repo), "refs/heads/worktree-risky", exclude_refs=("refs/heads/worktree-risky",)
        )
        assert determined is False
        assert orphans == []

        blocked, err, _ = _run_guard("git branch -D worktree-risky", repo)
        assert blocked
        assert "探测失败" in err

    # -- content git was never asked to hold --------------------------------

    def test_ignored_unrecoverable_files_block_teardown(self, tmp_path):
        """`git status --porcelain` hides ignored files, so a worktree whose only
        unique content was .env / data/ read as clean. Those files are in no
        commit and on no remote: a teardown is the only way to lose them."""
        main_repo, wts = _repo_with_worktrees(tmp_path, {"wf_env": False})
        wt = wts["wf_env"]
        (wt / ".gitignore").write_text(".env\ndata/\n__pycache__/\n")
        _git(["add", ".gitignore"], wt)
        _git(["commit", "-m", "ignore rules"], wt)
        (wt / ".env").write_text("TOKEN=local-only\n")
        (wt / "data").mkdir()
        (wt / "data" / "local.db").write_text("local database\n")

        blocked, err, _ = _run_guard(f'rm -rf "{wt}"', main_repo)
        assert blocked
        assert ".env" in err

        blocked, err, _ = _run_guard(f'git worktree remove "{wt}"', main_repo)
        assert blocked
        assert ".env" in err

    def test_regenerable_ignored_noise_does_not_block(self, tmp_path):
        """The counterweight: caches and build output are not work. Blocking on
        __pycache__ would make the guard fire on every worktree that ever ran
        the test suite, and a guard that always fires stops being read."""
        main_repo, wts = _repo_with_worktrees(tmp_path, {"wf_cache": False})
        wt = wts["wf_cache"]
        (wt / ".gitignore").write_text("__pycache__/\n*.log\n")
        _git(["add", ".gitignore"], wt)
        _git(["commit", "-m", "ignore rules"], wt)
        (wt / "__pycache__").mkdir()
        (wt / "__pycache__" / "mod.cpython-312.pyc").write_text("cache\n")
        (wt / "run.log").write_text("log output\n")

        blocked, _, warnings = _run_guard(f'rm -rf "{wt}"', main_repo)
        assert not blocked
        assert any("不会删除" in w for w in warnings)

    # -- "the branch survives" only holds for a linked worktree -------------

    def test_self_contained_repo_under_worktrees_blocks(self, tmp_path):
        """A clone keeps its refs inside the directory being deleted, so the
        advisory "the branch stays, commits are still reachable" was not just
        useless there - it was false."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        clone = main_repo / ".claude" / "worktrees" / "wf_clone"
        clone.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "-q", str(main_repo), str(clone)], tmp_path)
        _git(["config", "user.email", "test@example.com"], clone)
        _git(["config", "user.name", "Test"], clone)
        _git(["checkout", "-q", "-b", "worktree-clonework"], clone)
        head = _commit_file(clone, "only-here.txt", "sole copy\n", "sole copy")

        blocked, err, _ = _run_guard(f'rm -rf "{clone}"', main_repo)
        assert blocked
        assert "独立仓库" in err
        assert head[:7] in err

    def test_self_contained_repo_fully_on_its_remote_is_removable(self, tmp_path):
        """Not an excuse to block every clone: when a remote-tracking ref already
        reaches everything, the copy outlives the directory."""
        main_repo = tmp_path / "main"
        _init_repo(main_repo)
        clone = main_repo / ".claude" / "worktrees" / "wf_clone"
        clone.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "-q", str(main_repo), str(clone)], tmp_path)

        blocked, _, _ = _run_guard(f'rm -rf "{clone}"', main_repo)
        assert not blocked

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _repo_with_orphan_branch(tmp_path) -> pathlib.Path:
        """Repo whose branch `worktree-risky` is the only ref reaching its tip."""
        repo = tmp_path / "main"
        _init_repo(repo)
        _git(["checkout", "-q", "-b", "worktree-risky"], repo)
        _commit_file(repo, "only-here.txt", "sole copy\n", "sole copy")
        _git(["checkout", "-q", "master"], repo)
        return repo


# ===========================================================================
# Safety Rule S2: Hardcoded secrets in Write/Edit
# ===========================================================================


class TestSafetyS2HardcodedSecrets:
    """S2: Hardcoded secrets and .env file write detection."""

    @pytest.mark.parametrize(
        "content,field",
        [
            ('password = "supersecret"', "password"),
            ("secret='abc123'", "secret"),
            ('api_key = "sk-abc123"', "api_key"),
            ('token="ghp_xxxxx"', "token"),
        ],
    )
    def test_hardcoded_secret_in_write_produces_warning(self, content: str, field: str):
        """Hardcoded secret assignment in Write content must produce warning."""
        state: dict = {}
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": "src/config.py", "content": content},
        }
        warnings = _check_workflow_reminders(event, state)
        assert any("硬编码" in w or "环境变量" in w for w in warnings), (
            f"No secret warning for field={field}, content={content!r}"
        )

    @pytest.mark.parametrize(
        "content,field",
        [
            ('password = "supersecret"', "password"),
            ('api_key = "sk-xxx"', "api_key"),
        ],
    )
    def test_hardcoded_secret_in_edit_produces_warning(self, content: str, field: str):
        """Hardcoded secret in Edit new_string must also produce warning."""
        state: dict = {}
        event = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/config.py", "new_string": content},
        }
        warnings = _check_workflow_reminders(event, state)
        assert any("硬编码" in w or "环境变量" in w for w in warnings)

    def test_env_placeholder_no_secret_warning(self):
        """os.environ.get usage is not flagged as a hardcoded secret."""
        state: dict = {}
        event = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "src/config.py",
                "content": "api_key = os.environ.get('API_KEY')",
            },
        }
        warnings = _check_workflow_reminders(event, state)
        assert not any("硬编码" in w for w in warnings)

    def test_write_to_env_file_produces_warning(self):
        """.env file writes must produce a gitignore reminder."""
        state: dict = {}
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/project/.env", "content": "API_KEY=secret"},
        }
        warnings = _check_workflow_reminders(event, state)
        assert any(".env" in w or "gitignore" in w.lower() for w in warnings)

    def test_edit_to_env_file_produces_warning(self):
        """.env file path in Edit also triggers the reminder."""
        state: dict = {}
        event = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/project/.env",
                "new_string": "NEW_KEY=value",
            },
        }
        warnings = _check_workflow_reminders(event, state)
        assert any(".env" in w or "gitignore" in w.lower() for w in warnings)

    def test_write_to_non_env_file_no_env_warning(self):
        """Writing to a regular .py file must not trigger the .env warning."""
        state: dict = {}
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": "src/main.py", "content": "print('hello')"},
        }
        warnings = _check_workflow_reminders(event, state)
        assert not any(".env" in w and "gitignore" in w.lower() for w in warnings)

    def test_non_write_edit_tool_no_s2_check(self):
        """S2 check must not apply to tools other than Write and Edit."""
        state = {"last_taskwall_view": time.time()}
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "echo password='secret'"},
        }
        warnings = _check_workflow_reminders(event, state)
        assert not any("硬编码" in w for w in warnings)


# ===========================================================================
# Regression: S1 heredoc false-positive (BUG-002)
# ===========================================================================


class TestSafetyS1HeredocFalsePositive:
    """Regression tests for BUG-002: S1 scanning heredoc content.

    Root cause: S1 regex was applied to the full command string including
    heredoc body, so a git commit message mentioning 'rm -Rf /' caused
    sys.exit(2) even though no dangerous command was actually executed.

    Fix: strip heredoc blocks from cmd before S1 scanning (cmd_for_s1).
    """

    def test_git_commit_heredoc_with_rm_rf_not_blocked(self):
        """git commit whose message mentions 'rm -Rf /' must not be blocked.

        This is the exact false-positive scenario: the commit message text
        lives inside a heredoc and is never executed as shell code.
        """
        cmd = (
            "git commit -m \"$(cat <<'EOF'\n"
            "fix: block rm -Rf / in safety check\n"
            "\n"
            "Root cause: rm -Rf / was not intercepted with uppercase -R flag.\n"
            "EOF\n"
            ')"'
        )
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        with patch.object(sys, "exit") as mock_exit:
            _check_workflow_reminders(event, state)
        mock_exit.assert_not_called()

    def test_git_commit_heredoc_drop_table_not_warned(self):
        """Commit message mentioning DROP TABLE must not produce a DB warning."""
        cmd = "git commit -m \"$(cat <<'EOF'\ndocs: explain why DROP TABLE orders was reverted\nEOF\n)\""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        warnings = _check_workflow_reminders(event, state)
        assert not any("DROP" in w or "数据库" in w for w in warnings)

    def test_actual_rm_rf_root_outside_heredoc_still_blocked(self):
        """A real 'rm -rf /' outside any heredoc must still trigger exit(2).

        Regression guard: the heredoc stripping must not disable real S1 checks.
        """
        cmd = "rm -rf /"
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write"):
                _check_workflow_reminders(event, state)
        mock_exit.assert_called_once_with(2)

    def test_heredoc_with_rm_rf_root_before_heredoc_still_blocked(self):
        """If 'rm -rf /' appears before the heredoc, it must still be blocked.

        The heredoc stripping only removes content inside heredoc delimiters;
        dangerous commands that precede the heredoc remain visible to S1.
        """
        cmd = "rm -rf / && git commit -m \"$(cat <<'EOF'\nsome safe message\nEOF\n)\""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        with patch.object(sys, "exit") as mock_exit:
            with patch.object(sys.stderr, "write"):
                _check_workflow_reminders(event, state)
        mock_exit.assert_called_once_with(2)


# ===========================================================================
# State persistence across calls
# ===========================================================================


class TestStatePersistence:
    """Verify that state mutations across multiple calls are coherent."""

    def test_memo_cooldown_state_persists(self):
        """last_memo_reminder persists between calls and suppresses duplicates."""
        state: dict = {"last_memo_reminder": 0}
        event = {
            "tool_name": "Agent",
            "tool_input": {"team_name": "dev", "prompt": "do work"},
        }
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            w1 = _check_workflow_reminders(event, state)
            ts_after_first = state["last_memo_reminder"]
            w2 = _check_workflow_reminders(event, state)

        assert any("task_memo_read" in w for w in w1)
        assert not any("task_memo_read" in w for w in w2)
        assert state["last_memo_reminder"] == ts_after_first  # Not updated again

    def test_taskwall_timer_state_persists(self):
        """last_taskwall_view persists and is used for staleness calculation."""
        now = time.time()
        state = {"last_taskwall_view": now - 1801}  # Just over 30 min
        event = {"tool_name": "Read"}
        _check_workflow_reminders(event, state)
        ts_reset = state["last_taskwall_view"]
        assert ts_reset >= now  # Timer was reset

        # Immediately after reset, no second warning
        w2 = _check_workflow_reminders(event, state)
        assert not any("距上次查看任务墙" in w for w in w2)

    def test_bottleneck_count_state_persists(self):
        """bottleneck_check_count accumulates correctly across calls."""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            for i in range(1, 55):
                _check_workflow_reminders(event, state)
        assert state["bottleneck_check_count"] == 54

    def test_leader_counter_state_persists_between_calls(self):
        """leader_consecutive_calls counter persists correctly across calls."""
        state: dict = {}
        event = {"tool_name": "Read"}
        for i in range(1, 6):
            _check_leader_doing_too_much(event, state)
            assert state["leader_consecutive_calls"] == i


# ===========================================================================
# Pipeline binding helpers: _get_running_pipeline_subtask
# ===========================================================================


def _make_pipeline_task(
    task_id: str = "task-1",
    stage_name: str = "Implement",
    subtask_id: str = "sub-1",
    current_idx: int = 0,
    extra_stages: list | None = None,
) -> dict:
    """Build a minimal task dict with a pipeline config for testing."""
    stages = [{"name": stage_name, "status": "running", "subtask_id": subtask_id}]
    if extra_stages:
        stages.extend(extra_stages)
    return {
        "id": task_id,
        "status": "running",
        "config": {
            "pipeline": {
                "type": "feature",
                "current_stage_index": current_idx,
                "stages": stages,
            }
        },
    }


class TestGetRunningPipelineSubtask:
    """Tests for _get_running_pipeline_subtask helper."""

    def test_returns_subtask_id_for_running_task_with_pipeline(self):
        """Returns subtask_id when an active team has a running task with pipeline."""
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {"data": [_make_pipeline_task()]}
        urlopen_mock = _make_urlopen_mock([teams_resp, tasks_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            subtask_id, parent_id, stage_name, next_stage = _get_running_pipeline_subtask(
                "http://localhost:8000"
            )
        assert subtask_id == "sub-1"
        assert parent_id == "task-1"
        assert stage_name == "Implement"

    def test_returns_next_stage_name_when_more_stages_exist(self):
        """next_stage_name is filled when additional non-skipped stages follow."""
        extra = [{"name": "Test", "status": "pending", "subtask_id": "sub-2"}]
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {"data": [_make_pipeline_task(extra_stages=extra)]}
        urlopen_mock = _make_urlopen_mock([teams_resp, tasks_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            _, _, _, next_stage = _get_running_pipeline_subtask("http://localhost:8000")
        assert next_stage == "Test"

    def test_returns_none_when_no_active_teams(self):
        """All four values are None when there are no active teams."""
        teams_resp = {"data": [{"id": "team-1", "status": "completed"}]}
        urlopen_mock = _make_urlopen_mock([teams_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            result = _get_running_pipeline_subtask("http://localhost:8000")
        assert result == (None, None, None, None)

    def test_returns_none_when_task_has_no_pipeline(self):
        """Returns (None, None, None, None) for tasks without a pipeline config."""
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {"data": [{"id": "task-1", "status": "running", "config": {}}]}
        urlopen_mock = _make_urlopen_mock([teams_resp, tasks_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            result = _get_running_pipeline_subtask("http://localhost:8000")
        assert result == (None, None, None, None)

    def test_returns_none_when_api_unavailable(self):
        """When the API raises an exception, returns (None, None, None, None)."""
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = _get_running_pipeline_subtask("http://localhost:8000")
        assert result == (None, None, None, None)


# ===========================================================================
# Connection Point 1: _bind_subtask_running
# ===========================================================================


class TestBindSubtaskRunning:
    """Tests for _bind_subtask_running — CP1: advisory-only pipeline stage detection on dispatch.

    pipeline 退役后（对齐 pipeline_gate.py:413-419）这个 helper 只读探测存量 pipeline
    并返回提示，绝不再 PUT running——原自动写库有 active_teams[0] 启发式错绑风险。
    """

    def test_returns_advisory_without_writing(self):
        """Detects a legacy subtask and returns advisory text; never PUTs running."""
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {"data": [_make_pipeline_task(subtask_id="sub-42", stage_name="Design")]}
        urlopen_mock = _make_urlopen_mock([teams_resp, tasks_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock), \
                patch("aiteam.hooks.workflow_reminder._api_call") as api_mock:
            msg = _bind_subtask_running("http://localhost:8000")
        api_mock.assert_not_called()
        assert msg is not None
        assert "sub-42" in msg
        assert "Design" in msg
        assert "不会自动" in msg  # advisory only, no write

    def test_returns_none_when_no_pipeline(self):
        """Returns None silently when no running pipeline is found."""
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {"data": [{"id": "t1", "status": "running", "config": {}}]}
        urlopen_mock = _make_urlopen_mock([teams_resp, tasks_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            msg = _bind_subtask_running("http://localhost:8000")
        assert msg is None

    def test_returns_none_when_api_unavailable(self):
        """Returns None (does not raise) when API is unreachable."""
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            msg = _bind_subtask_running("http://localhost:8000")
        assert msg is None

    def test_rule2_cp1_injects_bind_advisory_in_workflow_reminders(self):
        """CP1 advisory appears on agent dispatch, without any task-status write."""
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {
            "data": [
                _make_pipeline_task(
                    subtask_id="sub-99",
                    stage_name="Implement",
                )
            ]
        }
        # urlopen: active-task check (rule 2a) then CP1 read-only detection — no write
        urlopen_mock = _make_urlopen_mock([
            teams_resp, tasks_resp,   # active task check (rule 2a)
            teams_resp, tasks_resp,   # CP1 read-only detection
        ])
        state: dict = {}
        event = {
            "tool_name": "Agent",
            "tool_input": {"team_name": "dev-team", "name": "backend-dev"},
        }
        with patch("urllib.request.urlopen", side_effect=urlopen_mock), \
                patch("aiteam.hooks.workflow_reminder._api_call") as api_mock:
            warnings = _check_workflow_reminders(event, state)
        api_mock.assert_not_called()
        assert any("sub-99" in w and "不会自动" in w for w in warnings)


# ===========================================================================
# Connection Point 2: _advance_pipeline_on_completion
# ===========================================================================


class TestAdvancePipelineOnCompletion:
    """Tests for _advance_pipeline_on_completion — CP2: advisory-only on completion report.

    pipeline 退役后（对齐 pipeline_gate.py:413-419）这个 helper 只读探测存量 pipeline
    并返回提示，绝不再 PUT completed / POST advance——原自动写库有 SendMessage 完成关键词
    误判 + active_teams[0] 错绑双重缺陷。
    """

    def test_returns_next_stage_advisory_without_writing(self):
        """Detects a legacy pipeline with a following stage; advisory only, no write."""
        extra = [{"name": "Test", "status": "pending", "subtask_id": "sub-2"}]
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {"data": [_make_pipeline_task(subtask_id="sub-1", extra_stages=extra)]}
        urlopen_mock = _make_urlopen_mock([teams_resp, tasks_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock), \
                patch("aiteam.hooks.workflow_reminder._api_call") as api_mock:
            msg = _advance_pipeline_on_completion("http://localhost:8000")
        api_mock.assert_not_called()
        assert msg is not None
        assert "Test" in msg  # Next stage name
        assert "不会自动" in msg  # advisory only, no write

    def test_returns_last_stage_advisory_without_writing(self):
        """Detects a legacy pipeline at its last stage; advisory only, no write."""
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {"data": [_make_pipeline_task(subtask_id="sub-1")]}  # Only one stage
        urlopen_mock = _make_urlopen_mock([teams_resp, tasks_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock), \
                patch("aiteam.hooks.workflow_reminder._api_call") as api_mock:
            msg = _advance_pipeline_on_completion("http://localhost:8000")
        api_mock.assert_not_called()
        assert msg is not None
        assert "不会自动" in msg  # advisory only, no write
        assert "最后阶段" in msg

    def test_returns_none_when_no_pipeline(self):
        """Returns None when there is no active pipeline to advance."""
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {"data": [{"id": "t1", "status": "running", "config": {}}]}
        urlopen_mock = _make_urlopen_mock([teams_resp, tasks_resp])
        with patch("urllib.request.urlopen", side_effect=urlopen_mock):
            msg = _advance_pipeline_on_completion("http://localhost:8000")
        assert msg is None

    def test_returns_none_when_api_unavailable(self):
        """Returns None (does not raise) when API is unreachable."""
        with patch("urllib.request.urlopen", side_effect=Exception("no api")):
            msg = _advance_pipeline_on_completion("http://localhost:8000")
        assert msg is None

    def test_rule9_cp2_injects_advance_advisory_in_workflow_reminders(self):
        """CP2 advisory appears on completion report; asserts NO PUT/POST write happens."""
        extra = [{"name": "Review", "status": "pending", "subtask_id": "sub-r"}]
        task = _make_pipeline_task(subtask_id="sub-1", extra_stages=extra)
        teams_resp = {"data": [{"id": "team-1", "status": "active"}]}
        tasks_resp = {"data": [task]}
        agents_resp = {"data": []}
        write_calls: list[str] = []

        # Use URL-routing mock so responses don't depend on call order
        def url_router(req, timeout=None):
            url = getattr(req, "full_url", str(req))
            method = getattr(req, "method", "GET")
            if method in ("PUT", "POST"):
                write_calls.append(f"{method} {url}")
            if "/agents" in url:
                resp_data = agents_resp
            elif "/tasks" in url and method == "GET":
                resp_data = tasks_resp
            else:
                resp_data = teams_resp
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.read = MagicMock(return_value=json.dumps(resp_data).encode())
            return cm

        state: dict = {}
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "team-lead", "message": "任务已完成，请确认"},
        }
        with patch("urllib.request.urlopen", side_effect=url_router):
            warnings = _check_workflow_reminders(event, state)
        assert not write_calls, f"pipeline 退役后不应有写库调用，实测: {write_calls}"
        assert any("不会自动" in w and "Review" in w for w in warnings)


class TestReminderThrottles:
    """催办治理⑤：汇报格式提醒改为子agent会话专属 + 每会话最多 1 次（替代旧 3600s 全局节流）。"""

    def _completion_event(self, session_id: str = "member-throttle") -> dict:
        return {
            "tool_name": "SendMessage",
            "tool_input": {"to": "leader", "message": "x" * 101 + " 任务已完成"},
            "session_id": session_id,
        }

    def test_report_format_fires_for_member_session(self):
        """子agent会话（成员汇报）触发汇报格式提醒。"""
        state: dict = {}
        with patch("urllib.request.urlopen", side_effect=Exception("no api")), patch(
            "aiteam.hooks.workflow_reminder._is_subagent_session", return_value=True
        ):
            warnings = _check_workflow_reminders(self._completion_event(), state)
        assert any("汇报可能缺少标准字段" in w for w in warnings)

    def test_report_format_throttled_within_session(self):
        """同一会话内第二次不再重复提醒（每会话最多 1 次）。"""
        state: dict = {}
        with patch("urllib.request.urlopen", side_effect=Exception("no api")), patch(
            "aiteam.hooks.workflow_reminder._is_subagent_session", return_value=True
        ):
            first = _check_workflow_reminders(self._completion_event(), state)
            second = _check_workflow_reminders(self._completion_event(), state)
        assert any("汇报可能缺少标准字段" in w for w in first)
        assert not any("汇报可能缺少标准字段" in w for w in second)

    def test_report_format_excluded_for_leader_session(self):
        """非子agent（Leader/主会话）发出的完成类消息不触发汇报格式提醒。"""
        state: dict = {}
        with patch("urllib.request.urlopen", side_effect=Exception("no api")), patch(
            "aiteam.hooks.workflow_reminder._is_subagent_session", return_value=False
        ):
            warnings = _check_workflow_reminders(self._completion_event(), state)
        assert not any("汇报可能缺少标准字段" in w for w in warnings)


# ---------------------------------------------------------------------------
# taskwall sync: SendMessage completion branch is advisory-only (2026-07-14 fix)
# ---------------------------------------------------------------------------


class TestTaskwallSyncSendMessageAdvisory:
    """SendMessage completion keywords must NEVER auto-write task status.

    Regression guard for the 2026-07-14 incident: Leader's outbound
    instruction "完成后向我汇报" hit the substring match and the hook
    auto-PUT the in-progress task to completed, bypassing acceptance.
    The branch is now advisory-only: it reminds Leader to use task_update.
    """

    def _state_with_dispatch(self) -> dict:
        return {
            "last_dispatched_task_id": "task-123",
            "last_dispatched_task_title": "修复 hooks 误置 completed",
        }

    def test_completion_message_makes_no_task_write_and_emits_advisory(self):
        """Leader forward-looking instruction: no API write, advisory only."""
        event = {
            "tool_name": "SendMessage",
            "tool_input": {
                "to": "worker-1",
                "message": "完成后向我汇报，不要自己置 completed",
            },
        }
        state = self._state_with_dispatch()
        with patch("aiteam.hooks.workflow_reminder._api_call") as api_mock:
            warnings = _post_tool_taskwall_sync(event, state, project_id="proj-1")
        api_mock.assert_not_called()
        assert any(
            "检测到完成类消息" in w and "task_update" in w and "hook 不自动写库" in w
            for w in warnings
        )
        assert any("修复 hooks 误置 completed" in w for w in warnings)

    def test_state_cleared_after_single_advisory(self):
        """Remind once then clear tracking keys — no repeated nagging."""
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "leader", "message": "任务已完成，请验收"},
        }
        state = self._state_with_dispatch()
        with patch("aiteam.hooks.workflow_reminder._api_call") as api_mock:
            first = _post_tool_taskwall_sync(event, state, project_id="proj-1")
            second = _post_tool_taskwall_sync(event, state, project_id="proj-1")
        api_mock.assert_not_called()
        assert "last_dispatched_task_id" not in state
        assert "last_dispatched_task_title" not in state
        assert any("检测到完成类消息" in w for w in first)
        assert second == []

    def test_shutdown_message_suppresses_advisory_and_keeps_state(self):
        """is_shutdown exclusion: no advisory, no write, state untouched."""
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "worker-1", "message": "工作完成，shutdown 收队"},
        }
        state = self._state_with_dispatch()
        with patch("aiteam.hooks.workflow_reminder._api_call") as api_mock:
            warnings = _post_tool_taskwall_sync(event, state, project_id="proj-1")
        api_mock.assert_not_called()
        assert warnings == []
        assert state["last_dispatched_task_id"] == "task-123"

    def test_no_dispatched_task_means_no_advisory(self):
        """Without last_dispatched_task_id there is nothing to remind about."""
        event = {
            "tool_name": "SendMessage",
            "tool_input": {"to": "leader", "message": "阶段一 done"},
        }
        state: dict = {}
        with patch("aiteam.hooks.workflow_reminder._api_call") as api_mock:
            warnings = _post_tool_taskwall_sync(event, state, project_id="proj-1")
        api_mock.assert_not_called()
        assert warnings == []
