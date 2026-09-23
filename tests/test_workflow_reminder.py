"""Complete unit tests for aiteam.hooks.workflow_reminder.

Coverage targets:
- the task-wall check on a named Agent dispatch (and its CJK-aware matcher)
- the safety rule groups: S1 dangerous Bash warnings / S2 secrets in
  Write|Edit / S3 sensitive git add / S4 worktree teardown / S5 commit-time
  branch ownership / S6 dispatch model tier

Test philosophy: guilty-until-proven-innocent. Every rule has at least one
positive trigger test and one negative (non-trigger) test.
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
    _check_commit_branch_ownership,
    _check_dispatch_model_tier,
    _check_workflow_reminders,
    _commit_probe_cwd,
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
# Agent dispatched with a name -> is the work on the task wall?
# ===========================================================================


def _wall_router(team_tasks: list[dict], wall_tasks: list[dict], seen: list[str] | None = None):
    """urlopen stand-in that answers by path, recording every URL it was asked for."""

    def _urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if seen is not None:
            seen.append(url)
        path = url.split("?")[0]
        if path.endswith("/api/teams"):
            payload = _teams_response([{"id": "t1", "status": "active", "project_id": "proj-1"}])
        elif path.endswith("/api/teams/t1/tasks"):
            payload = _tasks_response(team_tasks)
        elif path.endswith("/task-wall"):
            payload = {"wall": {"short": wall_tasks}}
        else:
            payload = {}
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.read = MagicMock(return_value=json.dumps(payload).encode())
        return cm

    return _urlopen


class TestAgentTaskWallCheck:
    """A named Agent dispatch: task_create first when nothing runs, else match the wall."""

    _RUNNING = [{"status": "running", "title": "Build API"}]

    def _agent_event(self, prompt: str = "start working", session: str = "lead-1", **extra) -> dict:
        tool_input = {"prompt": prompt, "description": "d", "name": "dev", "model": "opus"}
        tool_input.update(extra)
        return {"tool_name": "Agent", "tool_input": tool_input, "session_id": session}

    def _run(self, event: dict, wall: list[dict], state: dict | None = None) -> list[str]:
        state = {} if state is None else state
        with patch("urllib.request.urlopen", side_effect=_wall_router(self._RUNNING, wall)):
            return _check_workflow_reminders(event, state, project_id="proj-1")

    def test_no_active_task_produces_taskwall_warning(self):
        """When API returns no running tasks, a task-wall creation reminder appears."""
        responses = [_teams_response([{"id": "t1", "status": "active", "name": "dev"}]), _tasks_response([])]
        with patch("urllib.request.urlopen", side_effect=_make_urlopen_mock(responses)):
            warnings = _check_workflow_reminders(self._agent_event(), {})
        assert any("task_create" in w for w in warnings)

    def test_running_task_exists_no_taskwall_warning(self):
        """When a running task exists, no task-wall creation reminder is produced."""
        responses = [_teams_response([{"id": "t1", "status": "active"}]), _tasks_response(self._RUNNING)]
        with patch("urllib.request.urlopen", side_effect=_make_urlopen_mock(responses)):
            warnings = _check_workflow_reminders(self._agent_event(), {})
        assert not any("无进行中任务" in w for w in warnings)

    def test_api_unavailable_says_nothing(self):
        """An unreachable API is not evidence of anything: no advisory, no crash."""
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            warnings = _check_workflow_reminders(self._agent_event(), {}, project_id="proj-1")
        assert warnings == []

    def test_agent_without_name_or_team_makes_no_request(self):
        """No name, no team: nothing to match, so the API is never asked."""
        event = {"tool_name": "Agent", "tool_input": {"prompt": "x", "subagent_type": "Explore"}}
        with patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP expected")):
            assert _check_workflow_reminders(event, {}) == []

    def test_retired_template_and_memo_nudges_are_gone(self):
        """#42/#43: no agent_template_recommend nudge, no task_memo_read nudge."""
        event = self._agent_event(subagent_type="general-purpose", team_name="dev")
        warnings = self._run(event, [{"id": "aa11bb22-x", "status": "running", "title": "Build API"}])
        assert not any("agent_template_recommend" in w or "task_memo_read" in w for w in warnings)

    def test_chinese_prompt_matching_a_chinese_title_is_quiet(self):
        """D12: whitespace splitting never matched Chinese text, so this used to nag."""
        wall = [{"id": "aa11bb22-0001", "status": "running", "title": "workflow_reminder 提醒清理与守卫修订"}]
        event = self._agent_event(prompt="请做 workflow_reminder 的提醒清理，守卫修订按裁定执行。")
        assert not any("未匹配到任务墙" in w for w in self._run(event, wall))

    def test_chinese_title_without_latin_words_matches(self):
        wall = [{"id": "cc33dd44-0002", "status": "pending", "title": "用量平衡功能设计"}]
        event = self._agent_event(prompt="继续推进用量平衡的功能设计，先读现有方案。")
        assert not any("未匹配到任务墙" in w for w in self._run(event, wall))

    def test_task_id_in_prompt_matches(self):
        wall = [{"id": "ee55ff66-1234-5678", "status": "running", "title": "完全不同的标题"}]
        event = self._agent_event(prompt="task ee55ff66: follow up on the review")
        assert not any("未匹配到任务墙" in w for w in self._run(event, wall))

    def test_unrelated_work_still_gets_the_advisory(self):
        wall = [{"id": "cc33dd44-0002", "status": "pending", "title": "用量平衡功能设计"}]
        event = self._agent_event(prompt="restyle the dashboard sidebar icons")
        warnings = self._run(event, wall)
        assert any("未匹配到任务墙" in w and "用量平衡功能设计" in w for w in warnings)

    def test_one_shared_character_is_not_a_match(self):
        """A single common CJK pair must not count as covering a title."""
        wall = [{"id": "cc33dd44-0002", "status": "pending", "title": "用量平衡功能设计与实现"}]
        event = self._agent_event(prompt="修复登录页面的样式问题，顺便看设计稿")
        assert any("未匹配到任务墙" in w for w in self._run(event, wall))

    def test_mismatch_advisory_is_throttled_per_session(self):
        """D14: the hour-long throttle is per session; it used to silence every session."""
        wall = [{"id": "cc33dd44-0002", "status": "pending", "title": "用量平衡功能设计"}]
        state: dict = {}
        first = self._run(self._agent_event("restyle icons", session="sA"), wall, state)
        again = self._run(self._agent_event("restyle icons", session="sA"), wall, state)
        other = self._run(self._agent_event("restyle icons", session="sB"), wall, state)
        assert any("未匹配到任务墙" in w for w in first)
        assert not any("未匹配到任务墙" in w for w in again)
        assert any("未匹配到任务墙" in w for w in other)
        assert "wall_match_reminder_at" not in state

    def test_each_resource_fetched_once(self):
        wall = [{"id": "cc33dd44-0002", "status": "pending", "title": "用量平衡功能设计"}]
        seen: list[str] = []
        with patch("urllib.request.urlopen", side_effect=_wall_router(self._RUNNING, wall, seen)):
            _check_workflow_reminders(self._agent_event("restyle icons"), {}, project_id="proj-1")
        assert len(seen) == len(set(seen)) == 3, seen


class TestTaskWallMatcher:
    """_match_tokens / _dispatch_matches_task in isolation."""

    def test_cjk_run_becomes_overlapping_pairs(self):
        assert wr._match_tokens("守卫修订") == {"守卫", "卫修", "修订"}

    def test_latin_words_are_kept_whole_and_stop_words_dropped(self):
        assert wr._match_tokens("Fix the hook_core parser") == {"fix", "hook_core", "parser"}

    def test_function_characters_split_a_run(self):
        assert "的提" not in wr._match_tokens("任务的提醒")

    def test_empty_title_never_matches(self):
        assert not wr._dispatch_matches_task({"id": "", "title": ""}, "anything", {"anything"})


# ===========================================================================
# Safety Rule S1: Dangerous Bash commands (warnings; nothing here blocks)
# ===========================================================================


class TestSafetyS1DangerousBash:
    """S1: dangerous Bash commands get a warning.

    Recursive delete of the root or home directory itself is left to Claude
    Code's own dangerous-removal check, which parses the command and asks the
    user even in bypass mode. The regex that used to hard-block it here fired on
    home subdirectories and on quoted text (9 blocks, all of them false).
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            pytest.param("rm -rf /", id="root"),
            pytest.param("rm -rf ~/", id="home-slash"),
            pytest.param("rm -rf ~", id="home"),
            pytest.param("rm -r /", id="root-r"),
            pytest.param("rm -Rf /", id="root-upper-R"),
            pytest.param("rm -rf ~/Desktop/facet-export", id="home-subdir"),
            pytest.param("rm -rf ~/.cache/chrome-headless", id="home-dot-subdir"),
            pytest.param("grep -n 'rm -rf ~' notes.txt", id="quoted-pattern"),
            pytest.param("python3 -c \"print('rm -rf /')\"", id="quoted-code"),
        ],
    )
    def test_root_home_and_subdirectory_deletes_are_not_blocked_here(self, cmd: str, tmp_path):
        blocked, err, warnings = _run_guard(cmd, tmp_path)
        assert not blocked, err
        assert not any("OS BLOCK" in w for w in warnings)

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
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}}
        warnings = _check_workflow_reminders(event, state)
        s1_keywords = ["危险", "rm -rf", "DROP", "force push", "chmod 777"]
        assert not any(any(kw in w for kw in s1_keywords) for w in warnings)


# ===========================================================================
# Safety Rule S3: git add sensitive files
# ===========================================================================


class TestSafetyS3GitAddSensitive:
    """S3: `git add` of a secret-bearing file is hard-blocked.

    Only the path operands of a real `git add` are judged, on their basename.
    The old check matched substrings of the whole command line: 27 blocks in two
    months, none of them a real secret.
    """

    @pytest.mark.parametrize(
        "cmd, hit",
        [
            pytest.param("git add .env", ".env", id="env"),
            pytest.param("git add server.pem", ".pem", id="pem"),
            pytest.param("git add ~/.ssh/id_rsa", "id_rsa", id="id-rsa"),
            pytest.param("git add secret.key", ".key", id="key"),
            pytest.param("git add config/.env.local", ".env", id="env-local-in-subdir"),
            pytest.param("git add '.env*'", ".env", id="env-glob"),
            pytest.param('git add "my dir/.env"', ".env", id="quoted-path-with-space"),
            pytest.param("git add -f -- .env", ".env", id="flags-and-double-dash"),
            pytest.param("git -C sub add .env", ".env", id="git-C"),
            pytest.param("git add a.py && git add .env", ".env", id="second-git-add"),
            pytest.param("git add a.py .env.example .env", ".env", id="second-operand"),
            pytest.param("git add a.py; git commit -qm x; git add .env", ".env", id="after-commit"),
            pytest.param("bash -lc 'cd sub && git add .env'", ".env", id="bash-lc"),
            pytest.param('bash -o pipefail -c "git add id_rsa"', "id_rsa", id="bash-o-c"),
            pytest.param('eval "git add server.pem"', ".pem", id="eval"),
            pytest.param("bash -c -- 'git add .env'", ".env", id="bash-c-double-dash"),
            pytest.param("sh -c -e 'git add .env'", ".env", id="sh-c-option-after-c"),
            pytest.param("bash <<< 'git add .env'", ".env", id="here-string"),
            pytest.param("bash -s <<<'git add .env'", ".env", id="here-string-joined"),
            pytest.param("git add .\\config\\.env", ".env", id="windows-backslash-path"),
            pytest.param(
                "git add C:\\Users\\me\\.ssh\\id_rsa", "id_rsa", id="windows-drive-path"
            ),
            pytest.param("git stage .env", ".env", id="git-stage-synonym"),
            pytest.param("git add ~/.ssh/id_ed25519", "id_ed25519", id="id-ed25519"),
            pytest.param('git add "$HOME/.env"', ".env", id="variable-dir-sensitive-name"),
        ],
    )
    def test_sensitive_operand_is_blocked(self, cmd: str, hit: str, tmp_path):
        blocked, err, _warnings = _run_guard(cmd, tmp_path)
        assert blocked, cmd
        assert "OS BLOCK" in err and hit in err

    @pytest.mark.parametrize(
        "cmd",
        [
            pytest.param('git add src/app.py && git commit -m "load .env lazily"', id="commit-message"),
            pytest.param('echo "never git add .env"', id="echo-string"),
            pytest.param("grep -rn 'git add .env' docs", id="grep-pattern"),
            pytest.param("git add .env.example", id="example-template"),
            pytest.param("git add config/.env.sample x.pem.template", id="sample-and-template"),
            pytest.param("git add .environment.ts", id="env-prefix-only"),
            pytest.param("git add -A ':!.env'", id="exclude-pathspec"),
            pytest.param("git diff -- .env && git add README.md", id="other-git-verb"),
            pytest.param("git add src/main.py", id="plain-source"),
            pytest.param("cat > .env.example <<'EOF'\ngit add .env\nEOF", id="heredoc-body"),
            pytest.param("git add .env.dist", id="dist-template"),
            pytest.param("git add ~/.ssh/id_rsa.pub ~/.ssh/id_ed25519.pub", id="public-keys"),
            pytest.param("bash -c 'cat' <<< 'git add .env'", id="here-string-is-data-for-c"),
            pytest.param("bash deploy.sh -c 'git add .env'", id="script-file-arguments"),
        ],
    )
    def test_text_mentions_and_templates_pass(self, cmd: str, tmp_path):
        blocked, err, _warnings = _run_guard(cmd, tmp_path)
        assert not blocked, err

    @pytest.mark.parametrize(
        "cmd",
        [
            pytest.param("git add $FILE", id="variable"),
            pytest.param('git add "$(cat changed.txt)"', id="command-substitution"),
            pytest.param("git ls-files -m | xargs git add", id="xargs"),
            pytest.param("find . -name '*.cfg' -exec git add {} +", id="find-exec"),
            pytest.param("git add --pathspec-from-file=list.txt", id="pathspec-from-file"),
            pytest.param('eval "git add $F"', id="eval-with-variable-operand"),
        ],
    )
    def test_runtime_operands_are_not_guessed_but_flagged(self, cmd: str, tmp_path):
        """The file only exists at runtime: never a block on a guess, one advisory instead."""
        blocked, err, warnings = _run_guard(cmd, tmp_path)
        assert not blocked, err
        flagged = [w for w in warnings if "运行时才确定" in w]
        assert len(flagged) == 1, warnings

    @pytest.mark.parametrize(
        "cmd",
        [
            pytest.param('git add "$SRC/app.py"', id="variable-dir-visible-name"),
            pytest.param('git add "$NAME.example"', id="variable-template"),
            pytest.param('eval "$CMD"', id="eval-without-visible-git-add"),
            pytest.param("git add src/app.py", id="literal"),
        ],
    )
    def test_visible_or_unrelated_operands_are_not_flagged(self, cmd: str, tmp_path):
        blocked, err, warnings = _run_guard(cmd, tmp_path)
        assert not blocked, err
        assert not any("运行时才确定" in w for w in warnings), warnings

    def test_git_add_credentials_produces_warning(self, tmp_path):
        """A credentials-looking name warns (a name is not proof), never blocks."""
        blocked, _err, warnings = _run_guard("git add credentials.json", tmp_path)
        assert not blocked
        assert any("credentials" in w.lower() for w in warnings)

    def test_credentials_in_a_commit_message_is_not_warned(self, tmp_path):
        blocked, _err, warnings = _run_guard('git add a.py && git commit -m "rotate credentials"', tmp_path)
        assert not blocked
        assert not any("凭据" in w for w in warnings)

    def test_non_bash_tool_git_add_not_checked(self):
        """S3 check only applies to Bash tool, not Write/Edit."""
        state: dict = {}
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
        state: dict = {}
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
        state: dict = {}
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

    Root cause: S1 was applied to the full command string including heredoc
    body, so a commit message mentioning a dangerous command was treated as
    that command. Fix: strip heredoc blocks before scanning (cmd_for_s1).
    """

    def test_git_commit_heredoc_with_rm_rf_not_blocked(self):
        """git commit whose message mentions 'rm -Rf /' must not be blocked."""
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

    def test_command_before_the_heredoc_is_still_scanned(self):
        """Stripping the heredoc body must not hide the command in front of it."""
        cmd = "rm -rf * && git commit -m \"$(cat <<'EOF'\nsome safe message\nEOF\n)\""
        state: dict = {}
        event = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        warnings = _check_workflow_reminders(event, state)
        assert any("通配符" in w for w in warnings)


# ---------------------------------------------------------------------------
# S5: commit-time branch ownership — probe-cwd resolution
#
# Regression root (2026-08-19, user-reported false block): the probe cwd was
# found by a regex that only saw `git -C <dir> commit`. A `cd <wt> && git commit`
# left the probe on the session cwd (the main checkout), so a commit made inside
# an isolated worktree was judged against the main repo's HEAD and hard-blocked
# as "landing on someone else's branch" - the exact isolation the guard tells
# you to create. The fix resolves the running cwd through the same cd/`git -C`
# machinery S4 uses; these tests pin both the unit resolver and the end-to-end
# verdict so the regex can never creep back.
# ---------------------------------------------------------------------------


def _seed_ownership_repo(tmp_path):
    """main repo on `feature/claimed`, plus a linked worktree on `my-own-branch`.

    Returns (main_toplevel, wt_path). The caller seeds a claim under
    main_toplevel to simulate another agent owning the main checkout's branch.
    """
    main = tmp_path / "main"
    _init_repo(main)
    _git(["checkout", "-b", "feature/claimed"], main)
    wt = tmp_path / "wt"
    _git(["worktree", "add", "-b", "my-own-branch", str(wt)], main)
    toplevel = _git_out(["rev-parse", "--show-toplevel"], main)
    return toplevel, main, wt


class TestCommitProbeCwd:
    """Unit: which directory a `git commit` on the line actually runs in."""

    def test_cd_prefix_chain_resolves_to_worktree(self, tmp_path):
        _, _, wt = _seed_ownership_repo(tmp_path)
        cmd = f"cd {wt} && git add . && git commit -m docs"
        assert _commit_probe_cwd(cmd, base_cwd=str(tmp_path / "main")) == str(wt)

    def test_git_dash_c_resolves_to_worktree(self, tmp_path):
        _, _, wt = _seed_ownership_repo(tmp_path)
        cmd = f"git -C {wt} commit --allow-empty -m docs"
        assert _commit_probe_cwd(cmd, base_cwd=str(tmp_path / "main")) == str(wt)

    def test_subshell_cd_resolves_to_worktree(self, tmp_path):
        _, _, wt = _seed_ownership_repo(tmp_path)
        cmd = f"(cd {wt} && git commit --allow-empty -m docs)"
        assert _commit_probe_cwd(cmd, base_cwd=str(tmp_path / "main")) == str(wt)

    def test_bare_commit_uses_base_cwd(self):
        assert _commit_probe_cwd("git commit -m x", base_cwd="/base") == "/base"

    def test_quoted_literal_is_not_a_commit(self):
        assert _commit_probe_cwd('echo "git commit -m x"', base_cwd="/base") is None

    def test_no_commit_returns_none(self):
        assert _commit_probe_cwd("git status && git log", base_cwd="/base") is None


class TestCommitBranchOwnership:
    """End-to-end verdicts of the S5 guard around worktree isolation."""

    @staticmethod
    def _claim(toplevel, agent_id, branch, age_s):
        return {"branch_ownership": {toplevel: {agent_id: {"branch": branch, "ts": time.time() - age_s}}}}

    def test_worktree_commit_via_cd_not_blocked(self, tmp_path):
        """The reported bug: cd into an isolated worktree, commit, get blocked."""
        toplevel, main, wt = _seed_ownership_repo(tmp_path)
        state = self._claim(toplevel, "agent-OTHER", "feature/claimed", 7200)
        cmd = f"cd {wt} && git add . && git commit -m docs"
        # No SystemExit, no warning: the commit lands on my-own-branch, which
        # nobody else claims - structurally cannot touch feature/claimed.
        assert _check_commit_branch_ownership({"session_id": "agent-ME"}, state, cmd, str(main)) == []

    def test_worktree_commit_via_git_dash_c_not_blocked(self, tmp_path):
        toplevel, main, wt = _seed_ownership_repo(tmp_path)
        state = self._claim(toplevel, "agent-OTHER", "feature/claimed", 7200)
        cmd = f"git -C {wt} commit --allow-empty -m docs"
        assert _check_commit_branch_ownership({"session_id": "agent-ME"}, state, cmd, str(main)) == []

    def test_bare_commit_on_others_active_claim_blocks(self, tmp_path):
        """True positive preserved: committing straight onto the main checkout
        whose branch another agent actively claims still hard-blocks."""
        toplevel, main, _ = _seed_ownership_repo(tmp_path)
        state = self._claim(toplevel, "agent-OTHER", "feature/claimed", 7200)
        with pytest.raises(SystemExit) as exc:
            _check_commit_branch_ownership({"session_id": "agent-ME"}, state, "git commit -m x", str(main))
        assert exc.value.code == 2

    def test_bare_commit_on_others_stale_claim_warns_not_blocks(self, tmp_path):
        """A claim older than ACTIVE_TTL degrades to a warning, never a block:
        a dead agent must not fence off a branch a successor is picking up."""
        toplevel, main, _ = _seed_ownership_repo(tmp_path)
        stale = wr._BRANCH_OWNERSHIP_ACTIVE_TTL + 3600
        state = self._claim(toplevel, "agent-OTHER", "feature/claimed", stale)
        warnings = _check_commit_branch_ownership({"session_id": "agent-ME"}, state, "git commit -m x", str(main))
        assert warnings and "已过期" in warnings[0]

    def test_own_claim_refreshes_silently(self, tmp_path):
        toplevel, main, _ = _seed_ownership_repo(tmp_path)
        state = self._claim(toplevel, "agent-ME", "feature/claimed", 100)
        before = state["branch_ownership"][toplevel]["agent-ME"]["ts"]
        warnings = _check_commit_branch_ownership({"session_id": "agent-ME"}, state, "git commit -m x", str(main))
        assert warnings == []
        assert state["branch_ownership"][toplevel]["agent-ME"]["ts"] >= before

    def test_first_commit_records_claim(self, tmp_path):
        toplevel, main, _ = _seed_ownership_repo(tmp_path)
        state: dict = {}
        warnings = _check_commit_branch_ownership({"session_id": "agent-ME"}, state, "git commit -m x", str(main))
        assert warnings == []
        assert state["branch_ownership"][toplevel]["agent-ME"]["branch"] == "feature/claimed"


# ===========================================================================
# Safety Rule S6: dispatch model tier gate
# ===========================================================================


def _s6(tool_name: str, tool_input: dict) -> list[str]:
    return _check_dispatch_model_tier({"tool_name": tool_name, "tool_input": tool_input})


def _s6_block_stderr(tool_name: str, tool_input: dict, capsys) -> str:
    """Assert the dispatch is hard-blocked, return what the block said."""
    with pytest.raises(SystemExit) as exc:
        _s6(tool_name, tool_input)
    assert exc.value.code == 2
    return capsys.readouterr().err


class TestS6DispatchModelTier:
    """S6: every dispatch names its tier out loud; fable needs a written reason."""

    # ---- A. Agent tool -------------------------------------------------

    @pytest.mark.parametrize("tool_input", [
        {"prompt": "扫一遍 src/"},
        {"prompt": "扫一遍 src/", "model": ""},
        {"prompt": "扫一遍 src/", "model": "   "},
        {"prompt": "扫一遍 src/", "model": None},
    ])
    def test_agent_without_model_blocks(self, tool_input, capsys):
        """No model is not a default - it inherits the caller's tier."""
        err = _s6_block_stderr("Agent", tool_input, capsys)
        assert "OS BLOCK" in err
        assert "model" in err
        assert "不要重放" in err

    @pytest.mark.parametrize("model", ["opus", "Opus", "claude-opus-5", "opus[1m]"])
    def test_agent_opus_passes_silently(self, model):
        assert _s6("Agent", {"prompt": "扫一遍 src/", "model": model}) == []

    def test_agent_fable_without_reason_blocks(self, capsys):
        err = _s6_block_stderr("Agent", {"prompt": "终审这份设计", "model": "fable"}, capsys)
        assert "OS BLOCK" in err
        assert "fable 理由" in err

    @pytest.mark.parametrize("prompt", [
        "[fable 理由: 终审裁决需最强模型]\n请复核……",
        "[fable 理由：终审裁决需最强模型]\n请复核……",       # full-width colon
        "[ fable  理由 ： 对抗裁决 ]\n请复核……",              # padded marker
        "\n\n[fable 理由: 最高难度修复]\n请复核……",           # leading blank lines
    ])
    def test_agent_fable_with_reason_passes(self, prompt):
        assert _s6("Agent", {"prompt": prompt, "model": "fable"}) == []

    @pytest.mark.parametrize("model", ["fable", "Fable", "claude-fable-5-1", "claude-fable-5-1[1m]"])
    def test_agent_fable_family_all_recognised(self, model, capsys):
        err = _s6_block_stderr("Agent", {"prompt": "干活", "model": model}, capsys)
        assert "fable 理由" in err

    def test_agent_fable_reason_buried_deep_blocks(self, capsys):
        """The marker belongs on the first line, not buried in the task body."""
        prompt = "背景说明。" * 120 + "[fable 理由: 太晚了]"
        err = _s6_block_stderr("Agent", {"prompt": prompt, "model": "fable"}, capsys)
        assert "fable 理由" in err

    def test_agent_fork_without_reason_blocks(self, capsys):
        """fork ignores `model` and always inherits the parent - same as fable."""
        err = _s6_block_stderr(
            "Agent", {"prompt": "接着查", "subagent_type": "fork", "model": "opus"}, capsys
        )
        assert "fork" in err
        assert "fable 理由" in err

    def test_agent_fork_with_reason_passes(self):
        assert _s6("Agent", {
            "prompt": "[fable 理由: 需继承本会话上下文做终审]\n接着查",
            "subagent_type": "fork",
        }) == []

    def test_agent_fork_missing_model_is_not_the_missing_model_block(self, capsys):
        """A fork's model argument is ignored, so demanding one would be a lie."""
        err = _s6_block_stderr("Agent", {"prompt": "接着查", "subagent_type": "fork"}, capsys)
        assert "fork" in err

    @pytest.mark.parametrize("model", ["sonnet", "claude-sonnet-5", "haiku", "claude-haiku-4-5-20251001"])
    def test_agent_cheap_tier_warns_but_passes(self, model):
        warnings = _s6("Agent", {"prompt": "扫一遍", "model": model})
        assert warnings and model in warnings[0] and "派工策略" in warnings[0]
        assert not any("OS BLOCK" in w for w in warnings)

    def test_agent_unknown_model_passes_silently(self):
        """An unrecognised id is not evidence of a violation."""
        assert _s6("Agent", {"prompt": "干活", "model": "some-internal-eval-build"}) == []

    def test_other_tools_are_untouched(self):
        assert _s6("Bash", {"command": "ls"}) == []
        assert _s6("Read", {"file_path": "/tmp/x"}) == []

    # ---- B. Workflow tool ----------------------------------------------

    def test_workflow_agent_call_without_model_blocks(self, capsys):
        script = "const r = await agent('干活', { schema: S })\n"
        err = _s6_block_stderr("Workflow", {"script": script}, capsys)
        assert "OS BLOCK" in err
        assert "agent()" in err
        assert "不要重放" in err

    def test_workflow_counts_only_the_offending_call(self, capsys):
        script = (
            "const a = await agent('一', { model: 'opus' })\n"
            "const b = await agent('二', { schema: S })\n"
            "const c = await agent('三', { model: 'opus' })\n"
        )
        err = _s6_block_stderr("Workflow", {"script": script}, capsys)
        assert "1 处" in err       # exactly one offender
        assert "第 2 处" in err    # and it is the second call
        assert "共 3 处" in err

    def test_workflow_all_opus_passes_silently(self):
        script = (
            "const a = await agent('一' + WRITEBACK, { model: 'opus', schema: S })\n"
            "const b = await parallel(ITEMS.map(x => () => agent(p(x), { model: 'opus' })))\n"
        )
        assert _s6("Workflow", {"script": script}) == []

    def test_workflow_nested_parens_in_arguments_parse(self):
        script = "const a = await agent(build(x, y(z)), { model: 'opus' })\n"
        assert _s6("Workflow", {"script": script}) == []

    def test_workflow_quoted_model_key_is_not_a_false_block(self):
        script = 'const a = await agent(p, { "model": "opus" })\n'
        assert _s6("Workflow", {"script": script}) == []

    def test_workflow_agent_paren_inside_prompt_string_is_not_a_call(self):
        """Prompts routinely quote the words `agent(` - prose is not code."""
        script = (
            "const P = '每个 agent(x) 都要显式 model,别漏'\n"
            "const a = await agent(P, { model: 'opus' })\n"
        )
        assert _s6("Workflow", {"script": script}) == []

    def test_workflow_model_key_inside_prompt_string_is_not_a_declaration(self):
        """A prompt explaining the rule must not be read as obeying it, and a
        quoted 'fable' in prose must not count as a fable dispatch."""
        script = "const a = await agent('注意 model: \"fable\" 只给终审用', { model: 'opus' })\n"
        assert _s6("Workflow", {"script": script}) == []

    def test_workflow_escaped_quote_does_not_end_the_string_early(self):
        """An apostrophe inside a prompt must not hand the rest back to the scanner."""
        script = (
            "const P = 'don\\'t call agent(x) yourself'\n"
            "const a = await agent(P, { model: 'opus' })\n"
        )
        assert _s6("Workflow", {"script": script}) == []

    def test_workflow_agent_paren_inside_line_comment_is_not_a_call(self):
        script = (
            "// 旧写法 agent('x') 已废弃\n"
            "const a = await agent(P, { model: 'opus' })\n"
        )
        assert _s6("Workflow", {"script": script}) == []

    def test_workflow_agent_paren_inside_block_comment_is_not_a_call(self):
        script = (
            "/* 历史：agent('x') 曾经不带 model\n   多行说明 */\n"
            "const a = await agent(P, { model: 'opus' })\n"
        )
        assert _s6("Workflow", {"script": script}) == []

    def test_workflow_agent_paren_inside_multiline_template_is_not_a_call(self):
        script = (
            "const WRITEBACK = `回写说明\n"
            "第二行提到 agent(task) 但这是 prompt 文本\n"
            "第三行还有 agent(x, y)`\n"
            "const a = await agent('干活' + WRITEBACK, { model: 'opus' })\n"
        )
        assert _s6("Workflow", {"script": script}) == []

    def test_workflow_fable_call_with_matching_reason_comment_passes(self):
        script = (
            "const a = await agent('执行', { model: 'opus' })\n"
            "// fable 理由: 终审裁决需最强模型\n"
            "const v = await agent('终审', { model: 'fable', effort: 'xhigh' })\n"
        )
        warnings = _s6("Workflow", {"script": script})
        assert warnings and "fable" in warnings[0]
        assert not any("OS BLOCK" in w for w in warnings)

    def test_workflow_fable_calls_outnumbering_reasons_blocks(self, capsys):
        script = (
            "// fable 理由: 终审裁决需最强模型\n"
            "const v = await agent('终审', { model: 'fable' })\n"
            "const w = await agent('再来一次', { model: 'fable' })\n"
        )
        err = _s6_block_stderr("Workflow", {"script": script}, capsys)
        assert "2 处" in err
        assert "1 条" in err

    def test_workflow_fable_full_id_counts_as_fable(self, capsys):
        script = "const v = await agent('终审', { model: 'claude-fable-5-1' })\n"
        err = _s6_block_stderr("Workflow", {"script": script}, capsys)
        assert "fable" in err

    def test_workflow_reason_comment_accepts_full_width_colon(self):
        script = (
            "// fable 理由：对抗裁决\n"
            "const v = await agent('终审', { model: 'fable' })\n"
        )
        assert _s6("Workflow", {"script": script}) != []

    def test_workflow_without_inline_script_advises_not_blocks(self):
        """scriptPath / saved-workflow runs cannot be read here - say so, do not block."""
        for tool_input in ({}, {"script": ""}, {"name": "nightly-scan"}):
            warnings = _s6("Workflow", tool_input)
            assert warnings and "静态检查" in warnings[0]

    def test_workflow_script_without_any_agent_call_passes(self):
        assert _s6("Workflow", {"script": "const x = 1\nconsole.log(x)\n"}) == []

    # ---- C. Wiring and fail-safe ---------------------------------------

    def test_gate_runs_on_pretooluse_through_the_reminder_entrypoint(self):
        event = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {"prompt": "干活", "subagent_type": "explore"},
        }
        with pytest.raises(SystemExit) as exc:
            _check_workflow_reminders(event, {})
        assert exc.value.code == 2

    def test_gate_silent_on_posttooluse(self):
        """The agent already started - a verdict here answers nothing."""
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "tool_input": {"prompt": "干活", "subagent_type": "explore"},
        }
        warnings = _check_workflow_reminders(event, {})
        assert not any("OS BLOCK" in w or "S6" in w for w in warnings)

    def test_parser_defect_degrades_to_advisory_never_blocks(self):
        """A guard that crashes must not take every dispatch down with it."""
        with patch.object(wr, "_strip_script_noise", side_effect=RuntimeError("boom")):
            warnings = _s6("Workflow", {"script": "await agent('x', { model: 'opus' })"})
        assert warnings and "S6" in warnings[0] and "未能执行" in warnings[0]

    def test_malformed_tool_input_is_ignored(self):
        assert _check_dispatch_model_tier({"tool_name": "Agent", "tool_input": "not-a-dict"}) == []
        assert _check_dispatch_model_tier({"tool_name": "Agent"}) == []


class TestS6ModelNeutralWording:
    """S6 keeps both blocks (no model; fable without a reason) but prescribes no tier.

    Which tier suits which work is the user's own dispatch policy. The messages
    used to hard-code one ("执行层一律 opus", "fable 仅限终审…") and so pushed a
    policy the gate does not enforce.
    """

    _PRESCRIPTIVE = ("opus", "执行层", "终审", "对抗裁决", "最高难度")

    def _text_of(self, tool_name: str, tool_input: dict, capsys) -> str:
        try:
            warnings = _s6(tool_name, tool_input)
        except SystemExit as exc:
            assert exc.code == 2
            return capsys.readouterr().err
        assert warnings, (tool_name, tool_input)
        return "\n".join(warnings)

    @pytest.mark.parametrize(
        "tool_name, tool_input",
        [
            pytest.param("Agent", {"prompt": "干活"}, id="agent-no-model"),
            pytest.param("Agent", {"prompt": "干活", "model": "fable"}, id="agent-fable-no-reason"),
            pytest.param("Agent", {"prompt": "干活", "subagent_type": "fork"}, id="fork-no-reason"),
            pytest.param("Agent", {"prompt": "干活", "model": "sonnet"}, id="agent-sonnet"),
            pytest.param("Workflow", {"name": "saved-run"}, id="workflow-no-inline-script"),
            pytest.param("Workflow", {"script": "await agent('x', {label: 'a'})\n"}, id="wf-agent-no-model"),
            pytest.param(
                "Workflow", {"script": "await agent('x', { model: 'fable' })\n"}, id="wf-fable-no-reason"
            ),
            pytest.param(
                "Workflow",
                {"script": "// fable 理由: 需要\nawait agent('x', { model: 'fable' })\n"},
                id="wf-fable-with-reason",
            ),
        ],
    )
    def test_no_hard_coded_tier_policy(self, tool_name, tool_input, capsys):
        text = self._text_of(tool_name, tool_input, capsys)
        assert not any(word in text for word in self._PRESCRIPTIVE), text

    def test_parse_failure_advisory_is_neutral_too(self):
        with patch.object(wr, "_strip_script_noise", side_effect=RuntimeError("boom")):
            warnings = _s6("Workflow", {"script": "await agent('x', { model: 'fable' })"})
        assert warnings and not any(word in warnings[0] for word in self._PRESCRIPTIVE)
