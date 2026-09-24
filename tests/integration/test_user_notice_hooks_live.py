"""Hook exits against a real OS API process: what the user saw is what the ledger holds.

A real uvicorn API runs on a free port with its own HOME, database and release
cache (pre-seeded, so nothing goes to the network); ``AITEAM_API_URL`` points
every hook at it. The distributed hook scripts run as subprocesses the way
Claude Code runs them. State is read back through a separate HTTP client after
the hooks exited, and the central scenario restarts the API process on the same
database before reading, so the assertions cross the persistence boundary:

* session start shows ledger lines within budget plus one summary line; the
  next prompt shows what start had to defer; each exit reports the previous
  one's lines as written, and the delivery rows say so after a restart;
* a resume line Claude Code dropped is shown again at the next prompt, once,
  and that second showing starts the 24-hour cool-down; a resume line the
  transcript proves was shown is confirmed instead;
* with the API down, session start shows "service is not running" locally, and
  the next reachable prompt imports it into the ledger;
* the prompt exit passes its reader, so a channel mention shows up;
* a block rendered locally without HTTP reaches the ledger on the next fetch.

Nothing reaches port 8000 or the real HOME: HOME, the database, the cache and
the API address are all per test.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "plugin" / "hooks"
RELEASE_API = "https://api.github.com/repos/CronusL-1141/AI-company/releases/latest"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
HEADER_ZH = "AI Team OS 刚在界面上向用户显示了以下提示"


class LiveAPI:
    """One isolated API process that can be stopped and started on the same database."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.home = tmp_path / "home"
        (self.home / ".claude").mkdir(parents=True)
        cache = tmp_path / "cache" / "ai-team-os"
        cache.mkdir(parents=True)
        now = time.time()
        (cache / "release-check.json").write_text(json.dumps({
            "source": RELEASE_API, "version": "99.0.0", "tag": "v99.0.0", "checked_at": now, "attempted_at": now,
        }))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        env = {key: value for key, value in os.environ.items()
               if key not in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR", "LANG", "LANGUAGE", "LC_MESSAGES",
                              "CLAUDECODE", "CLAUDE_PROJECT_DIR")}
        env.update({
            "HOME": str(self.home), "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
            "AITEAM_DB_PATH": str(tmp_path / "core.db"), "AITEAM_API_URL": self.base,
            "XDG_CACHE_HOME": str(tmp_path / "cache"), "TMPDIR": str(tmp_path),
            "NO_PROXY": "127.0.0.1,localhost", "LC_ALL": "zh_CN.UTF-8", "CLAUDE_CODE_ENTRYPOINT": "cli",
        })
        self.env = env
        self.process: subprocess.Popen | None = None
        self.client = httpx.Client(base_url=self.base, timeout=10, trust_env=False)

    def start(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.tmp_path, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail("isolated API exited: " + self.process.stderr.read().decode(errors="replace")[-2000:])
            try:
                if self.client.get("/api/health").json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.05)
        pytest.fail("isolated API health timeout")

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:  # the port refuses before the next step runs
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    pass
            except OSError:
                return
            time.sleep(0.05)

    def restart(self) -> None:
        self.stop()
        self.start()

    def hook(self, script: str, payload: dict, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(HOOKS / script), *args], input=json.dumps(payload), text=True,
                              capture_output=True, env=self.env, cwd=str(cwd), timeout=60)

    def start_hook(self, session: str, source: str, cwd: Path, **extra) -> dict:
        result = self.hook("session_bootstrap.py", {"session_id": session, "source": source, "cwd": str(cwd),
                                                    "hook_event_name": "SessionStart", **extra}, cwd=cwd)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def prompt_hook(self, session: str, cwd: Path, **extra) -> dict:
        result = self.hook("channel_unread.py", {"session_id": session, "cwd": str(cwd), "prompt": "hi",
                                                 "hook_event_name": "UserPromptSubmit", **extra},
                           "leader-cc", cwd=cwd)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def notices(self) -> dict[str, dict]:
        rows = self.client.get("/api/notices", params={"status": "all", "limit": 100, "language": "zh"}).json()
        return {row["key"]: row for row in rows["items"]}

    def deliveries(self, key: str, session: str) -> list[dict]:
        detail = self.client.get(f"/api/notices/{key}").json()
        return [row for row in detail["deliveries"] if row["session_id"] == session]


@pytest.fixture()
def live(tmp_path):
    api = LiveAPI(tmp_path)
    api.start()
    try:
        yield api
    finally:
        api.stop()
        api.client.close()


def _project(api: LiveAPI, name: str = "project") -> Path:
    project = api.tmp_path / name
    project.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True, capture_output=True)
    return project


def _lines(document: dict) -> list[str]:
    text = document.get("systemMessage", "")
    return text.split("\n") if text else []


def _plain(line: str) -> str:
    return ANSI.sub("", line)


def _context(document: dict) -> str:
    return document.get("hookSpecificOutput", {}).get("additionalContext", "")


def _one_key(notices: dict[str, dict], catalog_id: str) -> str:
    keys = [key for key, row in notices.items() if row["catalog_id"] == catalog_id]
    assert len(keys) == 1, (catalog_id, sorted(notices))
    return keys[0]


def test_start_block_and_prompt_round_trip_through_the_ledger(live):
    project = _project(live)
    session = "live-session-1"

    document = live.start_hook(session, "startup", project)
    lines = _lines(document)
    assert all(line.startswith("[AI Team OS] ") for line in lines)
    release = [line for line in lines if "v99.0.0" in line]
    assert release, lines
    assert "新版 v99.0.0 可用" in release[0], "the ledger rendered in the hook's language"
    assert HEADER_ZH in _context(document)

    block = live.hook("workflow_reminder.py", {
        "session_id": session, "cwd": str(project), "tool_name": "Bash",
        "tool_input": {"command": "git add .env"}, "hook_event_name": "PreToolUse",
    }, "PreToolUse", cwd=project)
    assert block.returncode == 2
    assert json.loads(block.stdout)["systemMessage"] == (
        "[AI Team OS] \x1b[31m已拦截这条 git add：含敏感文件 .env，命令未执行\x1b[39m")
    assert block.stderr.endswith("用户界面已显示：[AI Team OS] 已拦截这条 git add：含敏感文件 .env，命令未执行")

    live.prompt_hook(session, project)

    notices = live.notices()
    blocked = [key for key in notices if key.startswith(f"blocked_secret_add:{session}:")]
    assert len(blocked) == 1, sorted(notices)
    assert [d["event"] for d in live.deliveries(blocked[0], session)] == ["local:PreToolUse"]
    released = _one_key(notices, "release_available")
    mine = live.deliveries(released, session)
    assert len(mine) == 1 and mine[0]["emitted_at"], "the prompt exit reported the start line as written"

    # A second start in the same session does not repeat the release line.
    again = live.start_hook(session, "resume", project)
    assert "v99.0.0" not in again.get("systemMessage", "")


def test_start_budget_carry_and_delivery_states_survive_an_api_restart(live):
    """Three start candidates: two lines plus a summary at start, the third at the first prompt."""
    project = _project(live)
    session = "live-budget"
    # A plugin install. The hook process has no CLAUDE_PLUGIN_ROOT (a main-chain
    # copy serves plugin users), and the API still gives the plugin command.
    plugins = live.home / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"ai-team-os@local": [{"version": "1.0.0", "installPath": str(ROOT / "plugin")}]}}))
    created = live.client.post("/api/leader-briefings", json={"title": "选 A 还是 B"})
    assert created.status_code == 200, created.text

    start = live.start_hook(session, "startup", project)
    lines = _lines(start)
    assert len(lines) == 3, lines
    assert lines[2] == "[AI Team OS] 另有 1 项待处理，对 Claude 说「列出 OS 提示」或打开 Dashboard 查看"
    assert {_plain(line) for line in lines[:2]} == {
        "[AI Team OS] 此目录未登记为项目，任务与记忆不会归档。对 Claude 说「注册」或「不用」",
        "[AI Team OS] 有 1 项等你决定，最新：选 A 还是 B。对 Claude 说「列出待决事项」",
    }
    assert all(line.startswith("[AI Team OS] \x1b[33m") and line.endswith("\x1b[39m") for line in lines[:2])
    start_context = _context(start)
    assert start_context.count(HEADER_ZH) == 3, "one model note per user line, the summary included"
    assert "notice_list" in start_context

    prompt = live.prompt_hook(session, project)
    assert _lines(prompt) == [
        "[AI Team OS] 新版 v99.0.0 可用（当前 v" + live.client.get("/api/health").json()["version"]
        + "）：claude plugin update ai-team-os，完成后重启 Claude Code",
    ]
    assert "claude plugin update ai-team-os" in _context(prompt)

    # The user is here: a decision parked now gets told to ask directly.
    parked = live.client.post("/api/leader-briefings", json={"title": "顺手问一句"}).json()
    assert parked.get("user_present") is True and "用户在场，请直接问" in parked["hint"]
    live.client.put(f"/api/leader-briefings/{parked['id']}/dismiss")

    # A new API process on the same database: nothing below comes from memory.
    live.restart()
    notices = live.notices()
    shown = {_plain(line) for line in lines[:2]} | {_plain(line) for line in _lines(prompt)}
    ledger_lines = {row["user_line"] for row in notices.values()
                    if row["catalog_id"] in ("unregistered_dir", "decisions_pending", "release_available")}
    assert shown == ledger_lines, "the ledger renders exactly what the user saw"

    for catalog_id in ("unregistered_dir", "decisions_pending"):
        key = _one_key(notices, catalog_id)
        detail = live.client.get(f"/api/notices/{key}").json()
        assert detail["rendered"]["zh"]["model_note"] in start_context
        (row,) = live.deliveries(key, session)
        assert row["event"] == "SessionStart:startup" and row["channel_reliable"] is True
        assert row["emitted_at"] and row["confirmed_at"], "the prompt exit reported them as written"
    release_key = _one_key(notices, "release_available")
    (row,) = live.deliveries(release_key, session)
    assert row["event"] == "UserPromptSubmit" and row["emitted_at"] is None, "not reported yet"

    # The next prompt reports it; the restarted API records it; nothing is shown twice.
    again = live.prompt_hook(session, project)
    assert "systemMessage" not in again
    (row,) = live.deliveries(release_key, session)
    assert row["emitted_at"] and row["confirmed_at"]


def _transcript(api: LiveAPI, name: str, records: list[dict]) -> Path:
    folder = api.home / ".claude" / "projects" / "-tmp-project"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def test_resume_line_the_transcript_shows_is_confirmed_not_repeated(live):
    project = _project(live)
    session = "live-resume-shown"
    resume = live.start_hook(session, "resume", project)
    (line,) = _lines(resume)
    assert "v99.0.0" in line
    assert "AI Team OS 尝试向用户显示以下提示，界面可能没有显示" in _context(resume)
    transcript = _transcript(live, session, [{
        "type": "attachment", "timestamp": _utc_stamp(),
        "attachment": {"type": "hook_system_message", "hookEvent": "SessionStart",
                       "hookName": "SessionStart:resume", "content": line},
    }])

    prompt = live.prompt_hook(session, project, transcript_path=str(transcript))
    assert "v99.0.0" not in prompt.get("systemMessage", "")
    (row,) = live.deliveries(_one_key(live.notices(), "release_available"), session)
    assert row["channel_reliable"] is False and row["confirmed_at"] and row["refired_at"] is None


def test_lost_resume_line_is_shown_again_once_and_then_cools_down(live):
    project = _project(live)
    session = "live-resume-lost"
    resume = live.start_hook(session, "resume", project)
    (line,) = _lines(resume)
    transcript = _transcript(live, session, [{"type": "user", "timestamp": _utc_stamp(),
                                              "message": {"role": "user", "content": "hi"}}])

    prompt = live.prompt_hook(session, project, transcript_path=str(transcript))
    assert _lines(prompt) == [line], "the prompt exit shows the lost start line again"
    assert HEADER_ZH in _context(prompt)
    key = _one_key(live.notices(), "release_available")
    (row,) = live.deliveries(key, session)
    assert row["refired_at"] and row["confirmed_at"] is None

    # Reported as written through a reliable exit: the cool-down starts, no third showing.
    assert "systemMessage" not in live.prompt_hook(session, project, transcript_path=str(transcript))
    (row,) = live.deliveries(key, session)
    assert row["confirmed_at"], "the refired line was confirmed once reported"
    other = live.start_hook("live-resume-next", "startup", project)
    assert "v99.0.0" not in other.get("systemMessage", ""), "24-hour cool-down across sessions"


def test_api_down_line_is_local_and_reaches_the_ledger_later(live):
    project = _project(live)
    session = "live-down"
    expected = "[AI Team OS] 服务未启动，任务墙与记忆暂不可用。重启 Claude Code，或对 Claude 说「重启 OS 服务」"
    live.stop()

    start = live.start_hook(session, "startup", project)
    assert _lines(start) == ["[AI Team OS] \x1b[33m" + expected[len("[AI Team OS] "):] + "\x1b[39m"]
    assert "os_restart_api" in _context(start)
    assert live.prompt_hook(session, project) == {}, "startup showed it; the prompt stays quiet"

    live.start()
    live.prompt_hook(session, project)
    notices = live.notices()
    assert notices["api_down"]["status"] == "cleared" and notices["api_down"]["user_line"] == expected
    (row,) = live.deliveries("api_down", session)
    assert row["event"] == "local:SessionStart:startup" and row["emitted_at"] and row["confirmed_at"]

    # Down again after it was up in between: a new session sees the line again.
    live.stop()
    assert _lines(live.start_hook("live-down-2", "startup", project)) == _lines(start)
    # A resume start may drop it: the model is told so, and the ledger does not call it confirmed.
    resumed = live.start_hook("live-down-3", "resume", project)
    assert _lines(resumed) == _lines(start)
    assert "界面可能没有显示" in _context(resumed)
    live.start()
    assert "systemMessage" not in live.prompt_hook("live-down-3", project)
    (row,) = live.deliveries("api_down", "live-down-3")
    assert row["event"] == "local:SessionStart:resume"
    assert row["channel_reliable"] is False and row["confirmed_at"] is None


def test_prompt_exit_passes_its_reader_and_shows_a_mention(live):
    project = _project(live)
    session = "live-mention"
    created = live.client.post("/api/projects", json={"name": "notice-e2e", "root_path": str(project)})
    assert created.status_code == 201, created.text
    project_id = created.json()["data"]["id"]
    sent = live.client.post(f"/api/channels/project:{project_id}/messages", json={
        "sender": "reviewer", "content": "please look", "mentions": ["leader-cc"], "project_id": project_id,
    })
    assert sent.status_code == 201, sent.text

    prompt = live.prompt_hook(session, project)
    channel = f"project:{project_id}"
    shown_channel = channel if len(channel) <= 20 else channel[:19] + "…"  # E10: channel up to 20 chars
    assert _lines(prompt) == [f"[AI Team OS] reviewer 在 {shown_channel} 点名你（1 条新消息），已交给 Claude 处理"]
    context = _context(prompt)
    assert "channel_read_ack(" in context and 'reader="leader-cc"' in context
    key = _one_key(live.notices(), "channel_mention")
    assert key.startswith(f"channel_mention:leader-cc:{project_id}:")
    (row,) = live.deliveries(key, session)
    assert row["event"] == "UserPromptSubmit"
