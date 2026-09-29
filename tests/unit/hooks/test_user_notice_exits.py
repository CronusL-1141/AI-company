"""The hook exits end to end: real hook scripts, real subprocesses, an isolated HOME.

Each case runs the distributed script (plugin/hooks) the way Claude Code does
(event JSON on stdin, argv from hooks.json) and reads what the user and the
model would get from its stdout, stderr and exit code. The API is either a
fake that validates the production request model, or a refused port.

Covers design §5.8: session start (ledger lines, or locally E24 / E01 / E02 /
E06), the resume/fork tick, the prompt exit (ledger lines, E01 fallback after an
unreliable start, the legacy channel badge for a pre-ledger API), blocks
E18-E21 as a deny whose reason is the user line (§14), E17 on a switched
branch, E22 on a held turn end, and the permission-denied hook filing nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ._notice_fakes import FakeApi, data_dir, hook_env, local_records, output, run_hook

E01_ZH = ("[AI Team OS] \x1b[33m服务未启动，任务墙与记忆暂不可用。重启 Claude Code，"
          "或对 Claude 说「重启 OS 服务」\x1b[39m")
STARTING_ZH = "[AI Team OS] OS 服务正在启动（MCP 会自动拉起，通常几秒）"
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


def test_start_without_api_says_it_is_starting_once_per_session(home):
    """A start that launched Claude Code: its MCP server brings the API up seconds later."""
    env = hook_env(home)
    first = _start(home, env)
    assert first.returncode == 0, first.stderr
    doc = output(first.stdout)
    assert doc["systemMessage"] == STARTING_ZH
    context = doc["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("AI Team OS 刚在界面上向用户显示了以下提示")
    assert "不要马上调用 os_restart_api" in context
    again = _start(home, env, source="resume")
    assert again.returncode == 0 and again.stdout == "", "same session: not again"
    other = _start(home, env, session="s2")
    assert output(other.stdout)["systemMessage"] == STARTING_ZH


def test_start_inside_a_running_claude_code_without_api_shows_e01(home):
    """/clear and compaction: nothing is starting the API, so E01 as before."""
    doc = output(_start(home, hook_env(home), source="compact").stdout)
    assert doc["systemMessage"] == E01_ZH
    context = doc["hookSpecificOutput"]["additionalContext"]
    assert "os_restart_api" in context and "uvicorn" in context


def test_colour_follows_the_entrypoint(home):
    plain = _start(home, hook_env(home, entrypoint=None), source="compact")
    assert output(plain.stdout)["systemMessage"] == E01_ZH.replace("\x1b[33m", "").replace("\x1b[39m", "")
    english = _start(home, hook_env(home, language="en_US.UTF-8"), session="s-en", source="compact")
    assert output(english.stdout)["systemMessage"].startswith("[AI Team OS] \x1b[33mService is not running")
    starting = _start(home, hook_env(home), session="s-status")
    assert output(starting.stdout)["systemMessage"] == STARTING_ZH, "a status line is never coloured"


def test_start_during_install_shows_e02_not_e01(home):
    state = data_dir(home) / "install-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"phase": "installing", "plugin_version": "1.14.0", "attempt": 2,
                                 "started_at": time.time() - 5, "pid": 1}))
    doc = output(_start(home, hook_env(home)).stdout)
    assert doc["systemMessage"] == "[AI Team OS] 正在安装依赖（第 2 次），装好之前 OS 工具不可用"
    (record,) = [r for r in local_records(home) if r["kind"] == "local_notice"]
    assert record["key"] == "install_in_progress:1.14.0:2"
    # A killed install (older than the pip budget) is not "in progress": the service is starting.
    state.write_text(json.dumps({"phase": "installing", "plugin_version": "1.14.0", "attempt": 2,
                                 "started_at": time.time() - 400, "pid": 1}))
    assert output(_start(home, hook_env(home), session="s2").stdout)["systemMessage"] == STARTING_ZH


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
    assert output(_start(home, hook_env(home)).stdout)["systemMessage"] == STARTING_ZH


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
# Resume / fork tick
# ---------------------------------------------------------------------------


def _tick(home: Path, env: dict, source: str):
    payload = {"session_id": "s1", "source": source, "cwd": str(home)}
    return run_hook("session_bootstrap.py", payload, env, "resume-tick", cwd=home)


def test_resume_tick_is_a_new_model_note_on_every_start(home):
    """CC drops a resume batch with nothing new in it, notice line included (design §14)."""
    with FakeApi() as api:
        env = hook_env(home, api.url)
        notes = []
        for source in ("resume", "resume", "fork"):
            result = _tick(home, env, source)
            assert result.returncode == 0, result.stderr
            doc = output(result.stdout)
            assert set(doc) == {"hookSpecificOutput"}, "model-only: the user line comes from the main hook"
            assert doc["hookSpecificOutput"]["hookEventName"] == "SessionStart"
            notes.append(doc["hookSpecificOutput"]["additionalContext"])
        assert len(set(notes)) == 3
        assert notes[0].startswith("[AI Team OS] 会话于 ") and notes[0].endswith("恢复（UTC）")
        assert notes[2].endswith("从原会话分叉（UTC）")
        assert api.requests == [], "the tick never calls the API"
    english = output(_tick(home, hook_env(home, language="en_US.UTF-8"), "fork").stdout)
    assert english["hookSpecificOutput"]["additionalContext"].startswith("[AI Team OS] Session forked at ")


@pytest.mark.parametrize("source", ["startup", "clear", "compact", ""])
def test_resume_tick_is_silent_on_other_starts(home, source):
    result = _tick(home, hook_env(home), source)
    assert result.returncode == 0 and result.stdout == ""


@pytest.mark.parametrize(
    ("main_chain", "ticks"),
    [
        pytest.param(None, True, id="no-main-chain"),
        pytest.param("", True, id="main-chain-without-the-tick"),
        pytest.param(" resume-tick", False, id="main-chain-ticks-itself"),
    ],
)
def test_the_plugin_tick_stands_down_only_for_a_main_chain_that_ticks(home, tmp_path, main_chain, ticks):
    """A main chain from before the tick registers session_bootstrap.py without it:
    yielding by script name there would drop the tick on every resume."""
    plugin = tmp_path / "plugin-root"
    (plugin / "hooks").mkdir(parents=True)
    source = Path(__file__).resolve().parents[3] / "plugin" / "hooks"
    for name in ("session_bootstrap.py", "user_notice.py"):
        (plugin / "hooks" / name).write_bytes((source / name).read_bytes())
    if main_chain is not None:
        runtime = home / ".claude" / "hooks" / "ai-team-os" / "session_bootstrap.py"
        command = f'"/usr/bin/python3" "{runtime}"{main_chain}'
        (home / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}))
    result = subprocess.run(
        [sys.executable, str(plugin / "hooks" / "session_bootstrap.py"), "resume-tick"],
        input=json.dumps({"session_id": "s1", "source": "resume", "cwd": str(home)}), text=True,
        capture_output=True, env=hook_env(home, CLAUDE_PLUGIN_ROOT=str(plugin)), cwd=str(home), timeout=30)
    assert result.returncode == 0, result.stderr
    assert bool(result.stdout.strip()) is ticks


def test_resume_tick_is_registered_for_resume_and_fork_only():
    manifest = json.loads((Path(__file__).resolve().parents[3] / "plugin/hooks/hooks.json").read_text("utf-8"))
    ticks = [group for group in manifest["hooks"]["SessionStart"]
             if any("resume-tick" in hook["command"] for hook in group["hooks"])]
    assert len(ticks) == 1 and len(ticks[0]["hooks"]) == 1
    assert sorted(ticks[0]["matcher"].split("|")) == ["fork", "resume"]
    others = [group for group in manifest["hooks"]["SessionStart"] if group is not ticks[0]]
    assert all("resume-tick" not in hook["command"] for group in others for hook in group["hooks"])


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
    assert output(_start(home, env, source="clear").stdout)["systemMessage"] == E01_ZH
    shown = _prompt(home, env)
    assert output(shown.stdout)["systemMessage"] == E01_ZH, "a /clear line may not have shown"
    assert _prompt(home, env).stdout == "", "and only once"


def test_a_fork_start_is_as_unreliable_as_a_resume(home):
    """Before CC 2.1.214 a fork reported "resume"; now it is its own source."""
    env = hook_env(home)
    doc = output(_start(home, env, source="fork").stdout)
    assert doc["systemMessage"] == STARTING_ZH
    assert doc["hookSpecificOutput"]["additionalContext"].startswith("AI Team OS 尝试向用户显示以下提示")
    (record,) = [r for r in local_records(home) if r["kind"] == "local_notice"]
    assert record["event"] == "SessionStart:fork"



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


def _assert_block(result, reason: str) -> dict:
    """CC 2.1.281 (design §14): exit 2 with a JSON deny. The reason is the user
    line, plain (the host shows it as the block's red line and gives it to the
    model); the [OS BLOCK] explanation is model-only context and also the stderr
    fallback CC reads only when the JSON is unusable. No systemMessage."""
    assert result.returncode == 2, result.stderr
    doc = output(result.stdout)
    assert set(doc) == {"hookSpecificOutput"}, "no systemMessage: it would repeat the reason"
    specific = doc["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse" and specific["permissionDecision"] == "deny"
    assert specific["permissionDecisionReason"] == reason
    assert specific["additionalContext"].startswith("[OS BLOCK]")
    assert result.stderr == specific["additionalContext"], "stderr is the same explanation, nothing else"
    return specific


def test_s3_block_states_its_reason_every_time_and_tells_the_model(home, repo):
    reason = "[AI Team OS] 已拦截这条 git add：含敏感文件 config/.env，命令未执行"
    with FakeApi() as api:
        env = hook_env(home, api.url)
        _assert_block(_tool(home, env, "Bash", {"command": "git add config/.env"}, repo), reason)
        _assert_block(_tool(home, env, "Bash", {"command": "git add config/.env"}, repo), reason)
        assert api.requests == [], "a block makes no HTTP call"
    blocks = [r for r in local_records(home) if r.get("catalog_id") == "blocked_secret_add"]
    assert len(blocks) == 1, "one Dashboard record per key and session"


def test_block_falls_back_to_stderr_when_the_notice_module_is_missing(home, repo, tmp_path):
    """Without user_notice the hook still blocks: exit 2 and the explanation on stderr."""
    hooks = tmp_path / "hooks-without-notice"
    hooks.mkdir()
    source = Path(__file__).resolve().parents[3] / "plugin" / "hooks" / "workflow_reminder.py"
    (hooks / "workflow_reminder.py").write_bytes(source.read_bytes())
    payload = {"session_id": "s1", "cwd": str(repo), "tool_name": "Bash",
               "tool_input": {"command": "git add config/.env"}, "hook_event_name": "PreToolUse"}
    result = subprocess.run([sys.executable, str(hooks / "workflow_reminder.py"), "PreToolUse"],
                            input=json.dumps(payload), text=True, capture_output=True,
                            env=hook_env(home), cwd=str(repo), timeout=30)
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr.startswith("[OS BLOCK]")


def test_s6_blocks_name_the_right_reason(home, repo):
    env = hook_env(home)
    no_model = _tool(home, env, "Agent", {"prompt": "do it", "subagent_type": "general-purpose"}, repo)
    _assert_block(no_model, "[AI Team OS] 已拦截派工：没有写明模型档位，Claude 需补上后重派")
    no_reason = _tool(home, env, "Agent", {"prompt": "do it", "model": "fable"}, repo)
    _assert_block(no_reason, "[AI Team OS] 已拦截派工：用 fable 或 fork 派工没有写理由，Claude 需补上后重派")
    english = _tool(home, hook_env(home, language="en_US.UTF-8"), "Agent", {"prompt": "do it"}, repo)
    _assert_block(english, "[AI Team OS] Blocked a dispatch: no model tier was given. "
                           "Claude must add it and dispatch again")


def test_s4_block_on_unsaved_work_and_on_an_undeterminable_target(home, repo):
    worktree = repo / ".worktrees" / "wip"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "wip")
    (worktree / "dirty.txt").write_text("unsaved")
    env = hook_env(home)
    unsaved = _tool(home, env, "Bash", {"command": f"git worktree remove --force {worktree}"}, repo)
    reason = output(unsaved.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    _assert_block(unsaved, reason)
    assert reason.startswith("[AI Team OS] 已拦截删除：") and "有未保存的工作" in reason
    unknown = _tool(home, env, "Bash", {"command": 'git branch -D "$BRANCH"'}, repo)
    reason = output(unknown.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    _assert_block(unknown, reason)
    assert "没能确认" in reason and "有未保存的工作" not in reason


def _seed_claims(home: Path, checkout: Path, claims: dict) -> None:
    state = data_dir(home) / "supervisor-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"branch_ownership": {str(checkout): claims}}))


def test_s5_foreign_branch_block(home, repo):
    _seed_claims(home, repo, {"other-agent": {"branch": "main", "ts": time.time() - 60}})
    result = _tool(home, hook_env(home), "Bash", {"command": "git commit -m x"}, repo)
    _assert_block(result, "[AI Team OS] 已拦截提交：分支 main 正被另一个会话使用，命令未执行")


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
    assert "systemMessage" not in source and "permissionDecisionReason" not in source
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


E22_NOTE = ("后台还有 3 项在运行，Claude 继续等待：Claude 需以后台任务方式运行 bash scripts/os-watch.sh "
            "<session_id> <team_id> 武装 watcher 后再停，或回复用户后收工；用户说「停」即结束。")


def _held(doc: dict) -> str:
    """A held turn end (design §14): the reason rides as additionalContext, never decision:block."""
    assert "decision" not in doc and "reason" not in doc
    specific = doc["hookSpecificOutput"]
    assert specific["hookEventName"] == "Stop"
    return specific["additionalContext"]


def test_blocked_turn_end_shows_e22_once_per_turn(home):
    with FakeApi() as api:
        api.routes[("GET", "/api/wake/actionable")] = lambda _: (200, {"busy_agents": 2, "live_runs": 1})
        env = hook_env(home, api.url)
        _new_turn(home, env)
        first = _stop(home, env)
        assert first.returncode == 0, first.stderr
        doc = output(first.stdout)
        line = "[AI Team OS] \x1b[31m还有 3 项在后台运行，已拦下收工让 Claude 继续等；说「停」即可结束\x1b[39m"
        assert doc["systemMessage"] == line
        # Shown to the user as "Stop hook feedback": plain words, no hedge about the line.
        assert _held(doc) == E22_NOTE
        second = output(_stop(home, env).stdout)
        assert "systemMessage" not in second and _held(second) == E22_NOTE, "the model hears it every time"
        _new_turn(home, env)
        third = output(_stop(home, env).stdout)
        assert third["systemMessage"] == line, "a new user turn shows it again"
        english = output(_stop(home, hook_env(home, api.url, language="en_US.UTF-8"), session="s-en").stdout)
        assert _held(english).startswith("3 background tasks are still running, so Claude keeps waiting: ")
        # Read by the user too: third person, no "you" aimed at the model.
        assert " you" not in _held(english).lower() and "你" not in _held(doc)


def test_held_turn_end_without_the_notice_module_still_holds(home, tmp_path):
    hooks = tmp_path / "hooks-without-notice"
    hooks.mkdir()
    source = Path(__file__).resolve().parents[3] / "plugin" / "hooks" / "turn_end_guard.py"
    (hooks / "turn_end_guard.py").write_bytes(source.read_bytes())
    with FakeApi() as api:
        api.routes[("GET", "/api/wake/actionable")] = lambda _: (200, {"busy_agents": 1, "live_runs": 0})
        result = subprocess.run(
            [sys.executable, str(hooks / "turn_end_guard.py")],
            input=json.dumps({"session_id": "s1", "cwd": str(home), "stop_hook_active": False}),
            text=True, capture_output=True, env=hook_env(home, api.url), cwd=str(home), timeout=30)
    assert result.returncode == 0
    assert "os-watch.sh" in _held(output(result.stdout))


def test_the_next_stop_after_a_hold_is_let_through(home):
    """stop_hook_active on the Stop that follows a hold: no output, no loop."""
    with FakeApi() as api:
        api.routes[("GET", "/api/wake/actionable")] = lambda _: (200, {"busy_agents": 1, "live_runs": 0})
        env = hook_env(home, api.url)
        payload = {"session_id": "s1", "cwd": str(home), "stop_hook_active": True}
        result = run_hook("turn_end_guard.py", payload, env, cwd=home)
        assert result.returncode == 0 and result.stdout == ""
        assert "/api/wake/actionable" not in api.paths()


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
