"""A session start that could not reach the API is owed to the next prompt (Claude Code side).

On a cold boot the start hook runs about 4s before the MCP server has brought
the API up (2026-09-29: four restored sessions all said "service not running"
and never got their briefing or direction memories). The start now says the
service is starting (E24) and leaves itself owed; the first prompt that reaches
the API shows what that start would have shown, built by the same code; a
prompt that still cannot reach it stays quiet during the starting grace and
says E01 after it. /clear and compaction keep E01 at the start: inside a
running Claude Code nothing is starting the API.

Real hook scripts in subprocesses (plugin/hooks), an isolated HOME, and a fake
API that validates the production request model.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import time
from pathlib import Path

import pytest

from ._notice_fakes import (
    PLUGIN_HOOKS,
    FakeApi,
    age_owed_starts,
    hook_env,
    local_records,
    output,
    owed_starts,
    run_hook,
)

STARTING_ZH = "[AI Team OS] OS 服务正在启动（MCP 会自动拉起，通常几秒）"
E01_ZH = ("[AI Team OS] \x1b[33m服务未启动，任务墙与记忆暂不可用。重启 Claude Code，"
          "或对 Claude 说「重启 OS 服务」\x1b[39m")
LINE = "[AI Team OS] \x1b[33m有 2 项等你决定，最新：测试。对 Claude 说「列出待决事项」\x1b[39m"
MEMORY = "OWED-START-DIRECTION-MEMORY"


@pytest.fixture()
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    return path


def _serve_briefing(api: FakeApi, home: Path, checkpoint: str = "") -> None:
    """A registered project with a team, one open task and one direction memory."""
    api.routes.update({
        ("GET", "/api/teams"): lambda _: (200, {"data": [{"name": "core", "status": "active"}]}),
        ("GET", "/api/projects"): lambda _: (200, {"data": [{"id": "p1", "root_path": str(home)}]}),
        ("GET", "/api/leader-briefings"): lambda _: (200, {"items": [], "total": 0}),
        ("POST", "/api/context/resolve"): lambda _: (200, {"project_id": "p1", "project": {"id": "p1"}}),
        ("GET", "/api/projects/p1/task-wall"): lambda _: (200, {"wall": {"short": [
            {"title": "fix the cold start", "priority": "high", "horizon": "short", "score": 9.0,
             "status": "pending"}]}, "stats": {}}),
        ("GET", "/api/memories"): lambda _: (200, {"data": [{"kind": "constraint", "content": MEMORY}]}),
        ("GET", "/api/hooks/compact-checkpoint"): lambda _: (
            200, {"found": True, "text": checkpoint} if checkpoint else {"found": False}),
    })
    api.pending = [{"language": "zh", "user_text": LINE, "model_text": "NOTE", "delivery_ids": ["d-1"]}]


def _start(home: Path, env: dict, session: str = "s1", source: str = "startup"):
    payload = {"session_id": session, "source": source, "cwd": str(home),
               "transcript_path": str(home / "t.jsonl")}
    result = run_hook("session_bootstrap.py", payload, env, cwd=home)
    assert result.returncode == 0, result.stderr
    return output(result.stdout)


def _prompt(home: Path, env: dict, session: str = "s1"):
    payload = {"session_id": session, "cwd": str(home), "prompt": "hi", "transcript_path": str(home / "t.jsonl")}
    started = time.monotonic()
    result = run_hook("channel_unread.py", payload, env, "leader-cc", cwd=home)
    assert result.returncode == 0, result.stderr
    return output(result.stdout), time.monotonic() - started


def _context(document: dict) -> str:
    return document.get("hookSpecificOutput", {}).get("additionalContext", "")


def _prompt_hook_timeout() -> float:
    """The timeout Claude Code kills the prompt hook at, as registered in hooks.json."""
    hooks = json.loads((PLUGIN_HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    (timeout,) = [hook["timeout"] for group in hooks["UserPromptSubmit"] for hook in group["hooks"]
                  if "channel_unread.py" in hook["command"]]
    return float(timeout)


def test_cold_start_says_starting_and_the_first_prompt_brings_the_same_start(home):
    down = hook_env(home)
    start = _start(home, down)
    assert start["systemMessage"] == STARTING_ZH, "a status line, not the action line E01"
    assert "不要马上调用 os_restart_api" in _context(start) and "服务未启动" not in _context(start)
    assert len(owed_starts(home)) == 1

    with FakeApi() as api:
        _serve_briefing(api, home)
        warm = _start(home, hook_env(home, api.url), session="warm")
        assert "Leader简报" in _context(warm) and MEMORY in _context(warm)
        assert len(owed_starts(home)) == 1, "a reachable start owes nothing"

        prompt, elapsed = _prompt(home, hook_env(home, api.url))
        assert prompt == {
            "systemMessage": warm["systemMessage"],
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": _context(warm)},
        }, "what the start would have shown, built by the same code"
        assert elapsed < _prompt_hook_timeout()
        start_fetch = api.pending_bodies()[-1]
        assert (start_fetch["event"], start_fetch["source"], start_fetch["session_id"]) == (
            "SessionStart", "startup", "s1")
        assert not owed_starts(home)

        again, _ = _prompt(home, hook_env(home, api.url))
        assert api.pending_bodies()[-1]["event"] == "UserPromptSubmit"
        assert "Leader简报" not in _context(again), "delivered once"
    (record,) = [r for r in local_records(home) if r["kind"] == "local_notice"]
    assert (record["catalog_id"], record["event"]) == ("api_starting", "SessionStart:startup")


def test_a_prompt_while_still_down_waits_out_the_grace_then_says_e01(home):
    down = hook_env(home)
    assert _start(home, down)["systemMessage"] == STARTING_ZH
    assert _prompt(home, down)[0] == {}, "the start said it is starting: not down yet"
    assert len(owed_starts(home)) == 1

    age_owed_starts(home)
    late, _ = _prompt(home, down)
    assert late["systemMessage"] == E01_ZH
    assert "os_restart_api" in _context(late)
    assert _prompt(home, down)[0] == {}, "E01 once per session"
    assert len(owed_starts(home)) == 1, "still owed: the briefing has not been delivered"

    with FakeApi() as api:
        _serve_briefing(api, home)
        recovered, _ = _prompt(home, hook_env(home, api.url))
        assert "Leader简报" in _context(recovered) and MEMORY in _context(recovered)
    assert not owed_starts(home)


def test_a_start_that_reaches_the_api_is_unchanged_and_owes_nothing(home):
    with FakeApi() as api:
        _serve_briefing(api, home)
        env = hook_env(home, api.url)
        start = _start(home, env)
        assert start["systemMessage"] == LINE
        assert _context(start).startswith("[AI Team OS] Session启动 — Leader简报")
        assert MEMORY in _context(start) and _context(start).endswith("\nNOTE")
        assert not owed_starts(home)
        prompt, _ = _prompt(home, env)
        assert prompt == {"systemMessage": LINE,
                          "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "NOTE"}}
        assert [body["event"] for body in api.pending_bodies()] == ["SessionStart", "UserPromptSubmit"]


@pytest.mark.parametrize("source", ["clear", "compact"])
def test_clear_or_compaction_without_api_says_e01_and_owes_its_start(home, source):
    """Inside a running Claude Code nothing is starting the API: E01 at once, no grace."""
    down = hook_env(home)
    start = _start(home, down, source=source)
    assert start["systemMessage"] == E01_ZH
    assert _context(start).startswith("AI Team OS 尝试向用户显示以下提示"), "clear and compact: unreliable"
    repeated, _ = _prompt(home, down)
    assert repeated["systemMessage"] == E01_ZH, "an unreliable start line is repeated once"
    assert _prompt(home, down)[0] == {}

    with FakeApi() as api:
        _serve_briefing(api, home, checkpoint="\nCHECKPOINT-TEXT")
        prompt, _ = _prompt(home, hook_env(home, api.url))
        context = _context(prompt)
        assert "Leader简报" in context and MEMORY in context
        assert ("CHECKPOINT-TEXT" in context) is (source == "compact")
        assert api.pending_bodies()[-1]["source"] == source
    assert not owed_starts(home)


def test_a_later_start_that_reaches_the_api_settles_the_owed_one(home):
    assert _start(home, hook_env(home))["systemMessage"] == STARTING_ZH
    with FakeApi() as api:
        _serve_briefing(api, home)
        env = hook_env(home, api.url)
        cleared = _start(home, env, source="clear")
        assert "Leader简报" in _context(cleared)
        assert not owed_starts(home)
        prompt, _ = _prompt(home, env)
        assert "Leader简报" not in _context(prompt), "not a second time"
        assert api.pending_bodies()[-1]["event"] == "UserPromptSubmit"


def _slow(routes: dict, seconds: float) -> None:
    for key, handler in list(routes.items()):
        routes[key] = lambda body, handler=handler: (time.sleep(seconds), handler(body))[1]


def test_a_slow_api_gives_the_owed_briefing_up_after_a_few_prompts(home):
    """A briefing that never fits the prompt budget costs a few slow prompts, not every prompt.

    Each briefing request takes 1.2s (five in a row: never done inside the 3.5s
    deadline); the notice fetch stays fast. Without the attempt limit every
    prompt waits about 3.5s and its own notices are put off for good.
    """
    notice = sys.modules["user_notice"]
    assert _start(home, hook_env(home))["systemMessage"] == STARTING_ZH
    mention = "[AI Team OS] reviewer 在 project:p1 点名你（1 条新消息），已交给 Claude 处理"
    with FakeApi() as api:
        _serve_briefing(api, home)
        _slow(api.routes, 1.2)
        api.pending = [{"language": "zh", "user_text": LINE, "model_text": "NOTE", "delivery_ids": [f"d-{n}"]}
                       for n in range(notice.OWED_ATTEMPTS)]
        api.pending.append({"language": "zh", "user_text": mention, "model_text": "MENTION",
                            "delivery_ids": ["d-mention"]})
        env = hook_env(home, api.url)
        for _ in range(notice.OWED_ATTEMPTS):
            late, _ = _prompt(home, env)
            assert late["systemMessage"] == LINE and "Leader简报" not in _context(late), "the notices go now"
            assert api.pending_bodies()[-1]["event"] == "SessionStart"
        assert not owed_starts(home), "given up"

        prompt, elapsed = _prompt(home, env)
        body = api.pending_bodies()[-1]
        assert (body["event"], body["reader"]) == ("UserPromptSubmit", "leader-cc")
        assert prompt["systemMessage"] == mention, "the prompt's own lines are back"
        assert elapsed < 1.5, f"an ordinary prompt again, not a 3.5s one: {elapsed:.2f}s"


def test_the_briefing_is_owed_per_session(home):
    down = hook_env(home)
    _start(home, down, session="a")
    _start(home, down, session="b")
    assert len(owed_starts(home)) == 2
    with FakeApi() as api:
        _serve_briefing(api, home)
        prompt, _ = _prompt(home, hook_env(home, api.url), session="a")
        assert "Leader简报" in _context(prompt)
    assert len(owed_starts(home)) == 1, "session b still waits for its own prompt"


def _load(name: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_HOOKS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_briefing_that_runs_out_of_time_waits_for_the_next_prompt(monkeypatch, capsys):
    """The hook keeps its own deadline: the notices go now, the briefing at the next prompt."""
    notice = sys.modules["user_notice"]
    start = _load("session_bootstrap.py", "session_bootstrap")
    monkeypatch.setitem(sys.modules, "session_bootstrap", start)
    hook = _load("channel_unread.py", "channel_unread_owed_test")
    monkeypatch.setattr(hook, "_OWED_DEADLINE_SECS", 0.2)
    monkeypatch.setattr(notice, "fetch_pending", lambda *a, **k: notice.Pending("zh", LINE, "NOTE", ["d-1"]))
    notice.mark_start_owed("cc", "slow", "startup")
    payload = json.dumps({"session_id": "slow", "cwd": "/tmp"})

    def run() -> dict:
        monkeypatch.setattr(notice, "_WROTE_DOCUMENT", False)
        monkeypatch.setattr(hook.sys, "argv", ["channel_unread.py", "leader-cc"])
        monkeypatch.setattr(hook.sys, "stdin", io.StringIO(payload))
        hook.main()
        return output(capsys.readouterr().out)

    monkeypatch.setattr(start, "startup_context", lambda info, source: time.sleep(2) or "BRIEFING")
    late = run()
    assert late == {"systemMessage": LINE,
                    "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "NOTE"}}
    assert notice.start_owed("cc", "slow") is not None, "still owed"

    monkeypatch.setattr(start, "startup_context", lambda info, source: "BRIEFING")
    assert _context(run()) == "BRIEFING\nNOTE"
    assert notice.start_owed("cc", "slow") is None
