"""CC 工具活动按 tool_use_id 在库里配对、补投标记、PostToolUseFailure 收尾：端到端。

真 uvicorn（httptools）+ 临时 SQLite 文件 + 真 send_event.py 子进程。补投标记
``_hook_replay`` 由 stdin 带进去，send_event 原样转发（它就是日后补投队列要走的路）。

覆盖的现场：
* 乱序：Post 先到、Pre 作为补投后到，只留一行且已完成，不留 running；
* 复活：补投的 Pre 不把 waiting 的 agent 拉成 busy，也不动 last_active_at，事件落在 origin_at；
* 同名交错：两个 Bash 并行，Pre A、Pre B、Post A、Post B，各归各行；
* 重启：Pre 之后 API 进程重启（内存 span 全丢），Post 仍按库里的行收尾；
* 失败：PostToolUseFailure 把 running 行收成 error 并带错误文本；
* 旧客户端：没有 tool_use_id 的 Pre/Post，以及「Pre 带 id、Post 被旧版剥掉 id」，都照旧配上。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn

from aiteam.api import app as app_module
from aiteam.api import debug_log, deps
from aiteam.api import event_bus as event_bus_module
from aiteam.api import middleware as middleware_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import hooks
from aiteam.api.ws.manager import ConnectionManager
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

ROOT = Path(__file__).resolve().parents[3]
SEND_EVENT = ROOT / "plugin" / "hooks" / "send_event.py"
SESSION = "00000000-0000-4000-8000-00000000b001"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """Start the real app on a temp database; ``api.restart()`` swaps in a fresh process state."""
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "0")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    monkeypatch.setattr(debug_log, "setup_debug_log", lambda: None)
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.delenv(hooks.HOOK_RAW_DUMP_ENV, raising=False)
    database = tmp_path / "pairing.sqlite"
    home = tmp_path / "home"
    home.mkdir()
    return _Api(database, home, monkeypatch)


class _Api:
    def __init__(self, database: Path, home: Path, monkeypatch) -> None:
        self.database = database
        self.home = home
        self._monkeypatch = monkeypatch
        self.port: int | None = None
        self._stack = None

    @contextmanager
    def running(self):
        """One API process lifetime: a new app, translator and in-memory state."""
        url = f"sqlite+aiosqlite:///{self.database}"
        app = app_module.create_app()
        monkeypatch = self._monkeypatch

        @asynccontextmanager
        async def lifespan(_app):
            repo = StorageRepository(db_url=url)
            await repo.init_db()
            bus = EventBus(repo=repo)
            translator = HookTranslator(repo=repo, event_bus=bus)
            app.dependency_overrides.update({
                deps.get_repository: lambda: repo,
                deps.get_event_bus: lambda: bus,
                deps.get_hook_translator: lambda: translator,
            })
            monkeypatch.setattr(deps, "_repository", repo)
            monkeypatch.setattr(deps, "_event_bus", bus)
            monkeypatch.setattr(deps, "_hook_translator", translator)
            try:
                yield
            finally:
                await translator.drain(5)
                await get_engine(url).dispose()

        app.router.lifespan_context = lifespan
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=0, http="httptools", log_level="warning", lifespan="on",
        ))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        self.port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield self
        finally:
            server.should_exit = True
            thread.join(10)

    def hook(self, event: str, payload: dict) -> subprocess.CompletedProcess:
        env = {**os.environ, "AITEAM_API_URL": f"http://127.0.0.1:{self.port}",
               "HOME": str(self.home)}
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        proc = subprocess.run(
            [sys.executable, str(SEND_EVENT), event], input=json.dumps(payload),
            capture_output=True, text=True, timeout=30, env=env, cwd=str(self.home),
        )
        assert proc.returncode == 0, proc.stderr
        assert "post_failed" not in proc.stderr, proc.stderr
        return proc

    def rows(self, sql: str, *args) -> list[tuple]:
        con = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def write(self, sql: str, *args) -> None:
        con = sqlite3.connect(self.database, timeout=5)
        try:
            con.execute(sql, args)
            con.commit()
        finally:
            con.close()


def _tool(api: _Api, tool_use_id: str | None, command: str, **extra) -> dict:
    payload = {"session_id": SESSION, "cwd": str(api.home), "tool_name": "Bash",
               "tool_input": {"command": command, "description": command}}
    if tool_use_id:
        payload["tool_use_id"] = tool_use_id
    return {**payload, **extra}


def _activities(api: _Api, marker: str) -> list[tuple]:
    return api.rows(
        "SELECT status, input_summary, output_summary, error, duration_ms, timestamp "
        "FROM agent_activities WHERE input_summary LIKE ? ORDER BY rowid", f"%{marker}%",
    )


def _db_time(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _marker() -> str:
    return uuid.uuid4().hex[:10]


def _start_session(api: _Api) -> None:
    """A registered project at the hook's cwd, so SessionStart creates the session leader."""
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{api.port}/api/projects",
        data=json.dumps({"name": "pairing", "root_path": str(api.home)}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 201
    api.hook("SessionStart", {"session_id": SESSION, "cwd": str(api.home), "source": "startup"})
    assert api.rows("SELECT role FROM agents WHERE session_id = ?", SESSION) == [("leader",)]


def _subagent(api: _Api) -> tuple[str, str]:
    """A registered subagent: (its cc agent_id, its row id)."""
    cc_id = f"toolu_{_marker()}"
    api.hook("SubagentStart", {"session_id": SESSION, "cwd": str(api.home),
                               "agent_id": cc_id, "agent_type": "backend-architect"})
    rows = api.rows("SELECT id FROM agents WHERE cc_tool_use_id = ?", cc_id)
    assert len(rows) == 1
    return cc_id, rows[0][0]


# ------------------------------------------------------------------ 乱序与复活


def test_completion_first_then_replayed_start_leaves_one_finished_row(api):
    with api.running():
        _start_session(api)
        m = _marker()
        origin = datetime.now(UTC) - timedelta(seconds=3)
        api.hook("PostToolUse", _tool(api, f"toolu_{m}", f"echo {m}", duration_ms=1200,
                                      tool_response={"stdout": f"out-{m}"}))
        api.hook("PreToolUse", _tool(api, f"toolu_{m}", f"echo {m}", _hook_replay={
            "origin_at": origin.isoformat(), "attempt": 1}))
    rows = _activities(api, m)
    assert [(r[0], r[2], r[4]) for r in rows] == [("completed", f"out-{m}", 1200)]
    assert abs((_db_time(rows[0][5]) - origin).total_seconds()) < 0.01  # moved back to the start


def test_replayed_start_does_not_revive_a_waiting_agent(api):
    with api.running():
        _start_session(api)
        cc_id, agent_id = _subagent(api)
        api.write("UPDATE agents SET status = 'waiting', last_active_at = '2020-01-01 00:00:00' "
                  "WHERE id = ?", agent_id)
        m = _marker()
        origin = datetime.now(UTC) - timedelta(minutes=5)
        api.hook("PreToolUse", _tool(api, f"toolu_{m}", f"echo {m}", agent_id=cc_id,
                                     agent_type="backend-architect",
                                     _hook_replay={"origin_at": origin.isoformat(), "attempt": 2}))
        status, last_active = api.rows(
            "SELECT status, last_active_at FROM agents WHERE id = ?", agent_id)[0]
        assert (status, last_active) == ("waiting", "2020-01-01 00:00:00")
        event_at, data = api.rows(
            "SELECT timestamp, data FROM events WHERE type = 'cc.tool_use' AND data LIKE ?",
            f"%{m}%")[0]
        assert abs((_db_time(event_at) - origin).total_seconds()) < 0.01
        assert json.loads(data)["hook_replay"]["attempt"] == 2
        assert abs((_db_time(_activities(api, m)[0][5]) - origin).total_seconds()) < 0.01

        # Control: the same call delivered live does revive it.
        live = _marker()
        api.hook("PreToolUse", _tool(api, f"toolu_{live}", f"echo {live}", agent_id=cc_id,
                                     agent_type="backend-architect"))
        assert api.rows("SELECT status FROM agents WHERE id = ?", agent_id)[0][0] == "busy"


def test_replayed_completion_does_not_touch_the_leader(api):
    """The leader touch (tool events mean the conversation is live) skips redeliveries."""
    with api.running():
        _start_session(api)
        api.write("UPDATE agents SET status = 'waiting', last_active_at = '2020-01-01 00:00:00' "
                  "WHERE session_id = ? AND role = 'leader'", SESSION)
        m = _marker()
        api.hook("PostToolUse", _tool(api, f"toolu_{m}", f"echo {m}", _hook_replay={
            "origin_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(), "attempt": 1}))
        assert api.rows("SELECT status, last_active_at FROM agents WHERE role = 'leader'") == [
            ("waiting", "2020-01-01 00:00:00")]
        api.hook("PostToolUse", _tool(api, f"toolu_{_marker()}", "echo live"))
        assert api.rows("SELECT status FROM agents WHERE role = 'leader'") == [("busy",)]


def test_bad_origin_falls_back_to_now(api):
    with api.running():
        _start_session(api)
        m = _marker()
        future = datetime.now(UTC) + timedelta(hours=2)
        before = datetime.now(UTC)
        api.hook("PreToolUse", _tool(api, f"toolu_{m}", f"echo {m}", _hook_replay={
            "origin_at": future.isoformat(), "attempt": 1}))
        event_at, data = api.rows(
            "SELECT timestamp, data FROM events WHERE type = 'cc.tool_use' AND data LIKE ?",
            f"%{m}%")[0]
    assert before - timedelta(seconds=1) <= _db_time(event_at) <= datetime.now(UTC)
    assert json.loads(data)["hook_replay"]["origin_valid"] is False


def test_replay_counters(api):
    stats = middleware_module.hook_ingest_stats
    before = dict(stats)
    with api.running():
        _start_session(api)
        m = _marker()
        live = _tool(api, f"toolu_{m}", f"echo {m}")
        api.hook("PreToolUse", live)
        # The client lost the receipt and redelivers: answered from the receipt.
        api.hook("PreToolUse", {**live, "_hook_replay": {
            "origin_at": datetime.now(UTC).isoformat(), "attempt": 1}})
        # A lifecycle event has no replay semantics yet: skipped, not handled.
        api.hook("SubagentStart", {"session_id": SESSION, "agent_id": f"toolu_{_marker()}",
                                   "agent_type": "backend-architect",
                                   "_hook_replay": {"origin_at": datetime.now(UTC).isoformat()}})
    assert stats["replayed"] - before["replayed"] == 2
    assert stats["replay_duplicate"] - before["replay_duplicate"] == 1
    assert stats["replay_skipped"] - before["replay_skipped"] == 1
    assert len(api.rows("SELECT 1 FROM events WHERE type = 'cc.tool_use' AND data LIKE ?",
                        f"%{m}%")) == 1


# ------------------------------------------------------------------ 同名交错与重启


def test_interleaved_calls_of_the_same_tool_pair_by_id(api):
    with api.running():
        _start_session(api)
        a, b = _marker(), _marker()
        api.hook("PreToolUse", _tool(api, f"toolu_{a}", f"echo {a}"))
        api.hook("PreToolUse", _tool(api, f"toolu_{b}", f"echo {b}"))
        api.hook("PostToolUse", _tool(api, f"toolu_{a}", f"echo {a}", tool_response={"stdout": f"out-{a}"}))
        api.hook("PostToolUse", _tool(api, f"toolu_{b}", f"echo {b}", tool_response={"stdout": f"out-{b}"}))
    assert [(r[0], r[2]) for r in _activities(api, a)] == [("completed", f"out-{a}")]
    assert [(r[0], r[2]) for r in _activities(api, b)] == [("completed", f"out-{b}")]


def test_pairing_survives_an_api_restart(api):
    m = _marker()
    with api.running():
        _start_session(api)
        api.hook("PreToolUse", _tool(api, f"toolu_{m}", f"echo {m}"))
    with api.running():  # a new process: every in-memory span is gone
        api.hook("PostToolUse", _tool(api, f"toolu_{m}", f"echo {m}", tool_response={"stdout": "done"}))
    assert [(r[0], r[2]) for r in _activities(api, m)] == [("completed", "done")]


# ------------------------------------------------------------------ PostToolUseFailure


def test_failure_closes_the_running_row_as_error(api):
    with api.running():
        _start_session(api)
        m = _marker()
        api.hook("PreToolUse", _tool(api, f"toolu_{m}", f"exit 3 {m}"))
        api.hook("PostToolUseFailure", _tool(api, f"toolu_{m}", f"exit 3 {m}", error="Exit code 3",
                                             is_interrupt=False, duration_ms=40))
    assert [(r[0], r[3], r[4]) for r in _activities(api, m)] == [("error", "Exit code 3", 40)]
    failed = api.rows("SELECT data FROM events WHERE type = 'cc.tool_failed'")
    assert [json.loads(d)["error"] for (d,) in failed] == ["Exit code 3"]


def test_interrupted_call_is_marked_so(api):
    with api.running():
        _start_session(api)
        m = _marker()
        api.hook("PreToolUse", _tool(api, f"toolu_{m}", f"sleep 60 {m}"))
        api.hook("PostToolUseFailure", _tool(api, f"toolu_{m}", f"sleep 60 {m}", error="",
                                             is_interrupt=True))
    assert [(r[0], r[3]) for r in _activities(api, m)] == [("error", "interrupted")]


# ------------------------------------------------------------------ 旧客户端不退化


def test_calls_without_tool_use_id_still_pair(api):
    with api.running():
        _start_session(api)
        m = _marker()
        api.hook("PreToolUse", _tool(api, None, f"echo {m}"))
        api.hook("PostToolUse", _tool(api, None, f"echo {m}", tool_response={"stdout": "legacy"}))
    assert [(r[0], r[2]) for r in _activities(api, m)] == [("completed", "legacy")]


def test_completion_stripped_of_its_id_by_an_old_hook_still_closes_the_row(api):
    """Pre carries the id; the old hook stripped it from an oversized PostToolUse."""
    with api.running():
        _start_session(api)
        m = _marker()
        api.hook("PreToolUse", _tool(api, f"toolu_{m}", f"echo {m}"))
        api.hook("PostToolUse", _tool(api, None, f"echo {m}", _stripped=True, _original_size=40000))
    assert [r[0] for r in _activities(api, m)] == ["completed"]


def test_socket_is_free_after_each_lifetime(api):
    """The restart test needs two servers on one database; make sure lifetimes do not leak."""
    with api.running():
        first = api.port
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", first))
    finally:
        probe.close()
