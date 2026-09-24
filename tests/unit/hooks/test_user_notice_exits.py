"""The hook exits end to end: real hook scripts, real subprocesses, an isolated HOME.

Each case runs the distributed script (plugin/hooks) the way Claude Code does
(event JSON on stdin, argv from hooks.json) and reads what the user and the
model would get from its stdout, stderr and exit code. The API is either a
fake that validates the production request model, or a refused port.

Covers design §5.8: session start (ledger lines, or locally E01 / E02 / E06),
the prompt exit (ledger lines, E01 fallback after an unreliable start, the
legacy channel badge for a pre-ledger API), blocks E18-E21 with the stderr
note, E17 on a switched branch, E22 on a blocked turn end, and the
permission-denied hook filing nothing.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from ._notice_fakes import FakeApi, data_dir, hook_env, local_records, output, run_hook

E01_ZH = ("[AI Team OS] \x1b[33m服务未启动，任务墙与记忆暂不可用。重启 Claude Code，"
          "或对 Claude 说「重启 OS 服务」\x1b[39m")
PENDING_LINE = "[AI Team OS] \x1b[33m有 2 项等你决定，最新：测试。对 Claude 说「列出待决事项」\x1b[39m"


@pytest.fixture()
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    return path


def _briefing_routes(api: FakeApi) -> None:
    api.routes.update({
        ("GET", "/api/teams"): lambda _: (200, {"data": []}),
        ("GET", "/api/projects"): lambda _: (200, {"data": []}),
        ("GET", "/api/leader-briefings"): lambda _: (200, {"items": [], "total": 0}),
        ("POST", "/api/context/resolve"): lambda _: (200, {"project_id": ""}),
        ("GET", "/api/memories"): lambda _: (200, {"data": []}),
        ("GET", "/api/hooks/compact-checkpoint"): lambda _: (200, {"found": False}),
    })


def _start(home: Path, env: dict, session: str = "s1", source: str = "startup", cwd: Path | None = None):
    payload = {"session_id": session, "source": source, "cwd": str(cwd or home),
               "transcript_path": str(home / "t.jsonl")}
    return run_hook("session_bootstrap.py", payload, env, cwd=cwd or home)


def _prompt(home: Path, env: dict, session: str = "s1", *args: str):
    payload = {"session_id": session, "cwd": str(home), "prompt": "hi", "transcript_path": str(home / "t.jsonl")}
    return run_hook("channel_unread.py", payload, env, *(args or ("leader-cc",)), cwd=home)


# ---------------------------------------------------------------------------
# Session start
# ---------------------------------------------------------------------------


def test_start_without_api_shows_e01_once_per_session(home):
    env = hook_env(home)
    first = _start(home, env)
    assert first.returncode == 0, first.stderr
    doc = output(first.stdout)
    assert doc["systemMessage"] == E01_ZH
    context = doc["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("AI Team OS 刚在界面上向用户显示了以下提示")
    assert "os_restart_api" in context and "uvicorn" in context
    again = _start(home, env, source="resume")
    assert again.returncode == 0 and again.stdout == "", "same session: not again"
    other = _start(home, env, session="s2")
    assert output(other.stdout)["systemMessage"] == E01_ZH


def test_colour_follows_the_entrypoint(home):
    plain = _start(home, hook_env(home, entrypoint=None))
    assert output(plain.stdout)["systemMessage"] == E01_ZH.replace("\x1b[33m", "").replace("\x1b[39m", "")
    english = _start(home, hook_env(home, language="en_US.UTF-8"), session="s-en")
    assert output(english.stdout)["systemMessage"].startswith("[AI Team OS] \x1b[33mService is not running")


def test_start_during_install_shows_e02_not_e01(home):
    state = data_dir(home) / "install-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"phase": "installing", "plugin_version": "1.14.0", "attempt": 2,
                                 "started_at": time.time() - 5, "pid": 1}))
    doc = output(_start(home, hook_env(home)).stdout)
    assert doc["systemMessage"] == "[AI Team OS] 正在安装依赖（第 2 次），装好之前 OS 工具不可用"
    (record,) = [r for r in local_records(home) if r["kind"] == "local_notice"]
    assert record["key"] == "install_in_progress:1.14.0:2"
    # A killed install (older than the pip budget) is not "in progress": E01 again.
    state.write_text(json.dumps({"phase": "installing", "plugin_version": "1.14.0", "attempt": 2,
                                 "started_at": time.time() - 400, "pid": 1}))
    assert output(_start(home, hook_env(home), session="s2").stdout)["systemMessage"] == E01_ZH


def _orphan_chain(home: Path, *, source_install: bool = False, disabled: bool = False) -> None:
    runtime = home / ".claude" / "hooks" / "ai-team-os"
    settings = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": f'"/usr/bin/python3" "{runtime}/session_bootstrap.py"'}]}]}}
    if disabled:
        settings["enabledPlugins"] = {"ai-team-os@ai-team-os": False}
        plugins = home / ".claude" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"ai-team-os@ai-team-os": [{"scope": "user"}]}}))
    (home / ".claude" / "settings.json").write_text(json.dumps(settings))
    if source_install:
        data_dir(home).mkdir(parents=True, exist_ok=True)
        (data_dir(home) / "install_path.txt").write_text("/somewhere/AI-company")


@pytest.mark.parametrize("disabled", [False, True], ids=["removed", "disabled"])
def test_start_with_a_leftover_global_chain_shows_e06(home, disabled):
    _orphan_chain(home, disabled=disabled)
    doc = output(_start(home, hook_env(home)).stdout)
    assert doc["systemMessage"] == (
        "[AI Team OS] \x1b[33m插件已卸载或停用，但全局 hook 仍在运行。对 Claude 说「清理 OS 残留」\x1b[39m")
    assert "uninstall_main_chain.py" in doc["hookSpecificOutput"]["additionalContext"]


def test_source_install_chain_is_not_a_leftover(home):
    _orphan_chain(home, source_install=True)
    assert output(_start(home, hook_env(home)).stdout)["systemMessage"] == E01_ZH


def test_start_with_api_shows_ledger_lines_and_reports_them_written(home):
    with FakeApi() as api:
        _briefing_routes(api)
        api.pending = [
            {"language": "zh", "user_text": PENDING_LINE, "model_text": "LEDGER-NOTE", "delivery_ids": ["d-1"]},
            {"language": "zh"},
        ]
        env = hook_env(home, api.url)
        result = _start(home, env, source="clear")
        assert result.returncode == 0, result.stderr
        doc = output(result.stdout)
        assert doc["systemMessage"] == PENDING_LINE
        context = doc["hookSpecificOutput"]["additionalContext"]
        assert "当前目录未注册为 OS 项目" in context, "the model briefing still rides along"
        assert context.endswith("\nLEDGER-NOTE")
        first = api.pending_bodies()[0]
        assert (first["event"], first["source"], first["session_id"]) == ("SessionStart", "clear", "s1")
        assert first["facts"]["entrypoint"] == "cli"
        assert first["transcript_path"] == str(home / "t.jsonl")
        # The next exit tells the ledger the line was written.
        assert _prompt(home, env).returncode == 0
        assert api.pending_bodies()[1]["facts"]["emitted"] == ["d-1"]
        assert api.pending_bodies()[1]["event"] == "UserPromptSubmit"


# ---------------------------------------------------------------------------
# Prompt exit
# ---------------------------------------------------------------------------


def test_prompt_shows_ledger_lines_with_reader_and_project(home):
    with FakeApi() as api:
        api.pending = [{"language": "zh", "user_text": PENDING_LINE, "model_text": "NOTE"}]
        result = _prompt(home, hook_env(home, api.url), "s1", "leader-cc", "proj-1")
        assert result.returncode == 0, result.stderr
        assert output(result.stdout) == {
            "systemMessage": PENDING_LINE,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "NOTE"},
        }
        (body,) = api.pending_bodies()
        assert (body["reader"], body["project_id"]) == ("leader-cc", "proj-1")
        assert "/api/channels/unread" not in api.paths()


def test_prompt_prints_nothing_when_the_ledger_has_nothing(home):
    with FakeApi() as api:
        result = _prompt(home, hook_env(home, api.url))
        assert result.returncode == 0 and result.stdout == ""


def test_prompt_falls_back_after_an_unreliable_start(home):
    env = hook_env(home)
    assert output(_start(home, env, source="resume").stdout)["systemMessage"] == E01_ZH
    shown = _prompt(home, env)
    assert output(shown.stdout)["systemMessage"] == E01_ZH, "a resume line may not have shown"
    assert _prompt(home, env).stdout == "", "and only once"


def test_prompt_trusts_a_reliable_start(home):
    env = hook_env(home)
    assert output(_start(home, env).stdout)["systemMessage"] == E01_ZH
    assert _prompt(home, env).stdout == ""


def test_prompt_on_a_pre_ledger_api_keeps_the_channel_badge(home):
    unread = {"data": {"reader": "leader-cc", "project_id": "p1", "total": 1, "truncated": False, "channels": [
        {"channel": "team:x", "count": 1, "latest_sender": "codex", "latest_excerpt": "hello",
         "latest_at": "2026-09-23T00:00:00Z"}]}}
    with FakeApi() as api:
        api.pending_status = 404
        api.routes[("GET", "/api/channels/unread")] = lambda _: (200, unread)
        result = _prompt(home, hook_env(home, api.url), "s1", "leader-cc", "p1")
        assert result.returncode == 0, result.stderr
        assert "[信道未读] 1 条消息点名 leader-cc" in result.stdout
        assert "systemMessage" not in result.stdout


def test_prompt_after_recovery_shows_e01_again_on_the_next_outage(home):
    env_down = hook_env(home)
    assert output(_prompt(home, env_down).stdout)["systemMessage"] == E01_ZH
    assert _prompt(home, env_down).stdout == ""
    with FakeApi() as api:
        assert _prompt(home, hook_env(home, api.url)).returncode == 0
    assert output(_prompt(home, env_down).stdout)["systemMessage"] == E01_ZH


# ---------------------------------------------------------------------------
# PreToolUse blocks (E18-E21) and the switched branch (E17)
# ---------------------------------------------------------------------------


def _tool(home: Path, env: dict, tool_name: str, tool_input: dict, cwd: Path, session: str = "s1"):
    payload = {"session_id": session, "cwd": str(cwd), "tool_name": tool_name, "tool_input": tool_input,
               "hook_event_name": "PreToolUse"}
    return run_hook("workflow_reminder.py", payload, env, "PreToolUse", cwd=cwd)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("a")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _assert_block(result, line: str) -> None:
    assert result.returncode == 2, result.stderr
    assert output(result.stdout) == {"systemMessage": line}
    plain = line.replace("\x1b[31m", "").replace("\x1b[39m", "")
    assert result.stderr.startswith("[OS BLOCK]")
    assert result.stderr.endswith("\n用户界面已显示：" + plain)


def test_s3_block_shows_one_red_line_and_tells_the_model(home, repo):
    with FakeApi() as api:
        env = hook_env(home, api.url)
        result = _tool(home, env, "Bash", {"command": "git add config/.env"}, repo)
        _assert_block(result, "[AI Team OS] \x1b[31m已拦截这条 git add：含敏感文件 config/.env，命令未执行\x1b[39m")
        again = _tool(home, env, "Bash", {"command": "git add config/.env"}, repo)
        assert again.returncode == 2 and again.stdout == ""
        assert "用户界面已显示" not in again.stderr, "no claim about a line that was not shown"
        assert api.requests == [], "a block makes no HTTP call"


def test_s6_blocks_name_the_right_reason(home, repo):
    env = hook_env(home)
    no_model = _tool(home, env, "Agent", {"prompt": "do it", "subagent_type": "general-purpose"}, repo)
    _assert_block(no_model, "[AI Team OS] \x1b[31m已拦截派工：没有写明模型档位，Claude 需补上后重派\x1b[39m")
    no_reason = _tool(home, env, "Agent", {"prompt": "do it", "model": "fable"}, repo)
    _assert_block(no_reason,
                  "[AI Team OS] \x1b[31m已拦截派工：用 fable 或 fork 派工没有写理由，Claude 需补上后重派\x1b[39m")


def test_s4_block_on_unsaved_work_and_on_an_undeterminable_target(home, repo):
    worktree = repo / ".worktrees" / "wip"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "wip")
    (worktree / "dirty.txt").write_text("unsaved")
    env = hook_env(home)
    unsaved = _tool(home, env, "Bash", {"command": f"git worktree remove --force {worktree}"}, repo)
    assert unsaved.returncode == 2
    line = output(unsaved.stdout)["systemMessage"]
    assert line.startswith("[AI Team OS] \x1b[31m已拦截删除：") and "有未保存的工作" in line
    unknown = _tool(home, env, "Bash", {"command": 'git branch -D "$BRANCH"'}, repo)
    assert unknown.returncode == 2
    assert "没能确认" in output(unknown.stdout)["systemMessage"]
    assert "有未保存的工作" not in output(unknown.stdout)["systemMessage"]


def _seed_claims(home: Path, checkout: Path, claims: dict) -> None:
    state = data_dir(home) / "supervisor-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"branch_ownership": {str(checkout): claims}}))


def test_s5_foreign_branch_block(home, repo):
    _seed_claims(home, repo, {"other-agent": {"branch": "main", "ts": time.time() - 60}})
    result = _tool(home, hook_env(home), "Bash", {"command": "git commit -m x"}, repo)
    _assert_block(result, "[AI Team OS] \x1b[31m已拦截提交：分支 main 正被另一个会话使用，命令未执行\x1b[39m")


def test_switched_branch_shows_e17_once_next_to_the_model_warning(home, repo):
    _seed_claims(home, repo, {"s1": {"branch": "feature", "ts": time.time() - 60}})
    env = hook_env(home)
    result = _tool(home, env, "Bash", {"command": "git commit -m x"}, repo)
    assert result.returncode == 0, result.stderr
    doc = output(result.stdout)
    assert doc["systemMessage"] == (
        "[AI Team OS] \x1b[33mrepo 的分支已从 feature 换成 main，可能有别的会话在用这个目录。"
        "对 Claude 说「查分支变更」\x1b[39m")
    context = doc["hookSpecificOutput"]["additionalContext"]
    assert "[安全] 分支所有权警告" in context and "reflog" in context
    again = output(_tool(home, env, "Bash", {"command": "git commit -m y"}, repo).stdout)
    assert "systemMessage" not in again, "once per session"
    assert "[安全] 分支所有权警告" in again["hookSpecificOutput"]["additionalContext"]


def test_workflow_reminder_source_has_no_direct_user_output():
    source = (Path(__file__).resolve().parents[3] / "plugin/hooks/workflow_reminder.py").read_text(encoding="utf-8")
    assert "systemMessage" not in source
    exits = [line for line in source.splitlines() if line.strip().startswith("sys.exit(2)")]
    assert len(exits) == 1, "every block goes through _block"


# ---------------------------------------------------------------------------
# Stop guard (E22)
# ---------------------------------------------------------------------------


def _stop(home: Path, env: dict, session: str = "s1"):
    return run_hook("turn_end_guard.py", {"session_id": session, "cwd": str(home), "stop_hook_active": False}, env,
                    cwd=home)


def _new_turn(home: Path, env: dict, session: str = "s1") -> None:
    assert run_hook("turn_end_guard.py", {"session_id": session, "cwd": str(home)}, env, "user-prompt",
                    cwd=home).returncode == 0
    state = data_dir(home) / "wake-state" / f"{session}.json"
    document = json.loads(state.read_text())
    document["manual_until"] = 0  # the user walked away: the guard applies again
    state.write_text(json.dumps(document))


def test_blocked_turn_end_shows_e22_once_per_turn(home):
    with FakeApi() as api:
        api.routes[("GET", "/api/wake/actionable")] = lambda _: (200, {"busy_agents": 2, "live_runs": 1})
        env = hook_env(home, api.url)
        _new_turn(home, env)
        first = _stop(home, env)
        assert first.returncode == 0, first.stderr
        doc = output(first.stdout)
        line = "[AI Team OS] \x1b[31m还有 3 项在后台运行，已拦下收工让 Claude 继续等；说「停」即可结束\x1b[39m"
        assert doc["decision"] == "block" and doc["systemMessage"] == line
        # Visibility of a systemMessage next to decision:block is unverified: the
        # model hears that the line was tried, never that the user saw it.
        plain = line.replace("\x1b[31m", "").replace("\x1b[39m", "")
        assert doc["reason"].endswith("\n已尝试在用户界面显示（可能未显示）：" + plain)
        assert "用户界面已显示" not in doc["reason"]
        second = output(_stop(home, env).stdout)
        assert second["decision"] == "block" and "systemMessage" not in second
        assert "已尝试在用户界面显示" not in second["reason"]
        _new_turn(home, env)
        third = output(_stop(home, env).stdout)
        assert third["systemMessage"] == line, "a new user turn shows it again"


# ---------------------------------------------------------------------------
# Permission denials file nothing
# ---------------------------------------------------------------------------


def test_permission_denial_files_no_pending_item(home):
    with FakeApi() as api:
        payload = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls /private"},
                   "reason": "path is outside the project", "tool_use_id": "t1", "cwd": str(home)}
        result = run_hook("permission_denied_recovery.py", payload, hook_env(home, api.url), cwd=home)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {"hookSpecificOutput": {"hookEventName": "PermissionDenied",
                                                                    "retry": False}}
        assert "/api/hooks/event" in api.paths()
        assert "/api/leader-briefings" not in api.paths()
