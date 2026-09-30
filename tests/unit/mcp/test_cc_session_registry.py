"""CC 会话 id 跟着父进程登记走，不停在 MCP 子进程启动时的环境变量上。

``claude --resume`` 续上旧对话后，CC 进程登记 ``<config>/sessions/<pid>.json`` 与 hook
用的都是被续上的对话 id，MCP 子进程环境变量里的 CLAUDE_CODE_SESSION_ID 却还是启动时
的一次性 id。拿后者当 ``X-CC-Session-Id``，记账动作的 agent→task 边会绑到那个只活了
几十毫秒的空壳 Leader 行上，子 agent 的边一条也建不出来。

每个回落用例都成对断言：先断言某一条校验把登记挡回环境变量，再只修好这一条，断言
登记生效。撤回登记读取，后一半必红——证明回落来自那条校验，而不是根本没读登记。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest

from aiteam.mcp import _base
from aiteam.mcp.tools import infra

STARTUP_ID = "91ca1de1-162f-47c5-a9e3-1510f4f7a667"  # the id CC started with (MCP env)
RESUMED_ID = "39c7d5a3-52c6-4735-b866-1fa68229709a"  # the conversation it resumed
CLEARED_ID = "5b8e0c7d-2f41-4d1e-9a3c-7e6f5d4c3b2a"  # a later /clear or /resume


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch, tmp_path):
    """Registry under tmp CLAUDE_CONFIG_DIR, fresh module caches, no inherited session env."""
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    # raising=False keeps these tests runnable against a build without the registry
    # (the revert check): they must fail on their assertions, not in setup.
    monkeypatch.setattr(_base, "_cc_registry_last_good", {}, raising=False)
    monkeypatch.setattr(_base, "_cc_parent_started_at", {}, raising=False)


def _registry_path(tmp_path: Path, pid: int | None = None) -> Path:
    return tmp_path / "claude" / "sessions" / f"{os.getppid() if pid is None else pid}.json"


def _registration(session_id: str, **overrides) -> dict:
    """The shape CC 2.1.285 writes (fields copied from a live registry)."""
    ppid = os.getppid()
    started_ms = int(psutil.Process(ppid).create_time() * 1000) + 2500
    record = {
        "pid": ppid,
        "sessionId": session_id,
        "cwd": "/work/repo",
        "startedAt": started_ms,
        "procStart": "Wed Sep 30 04:59:58 2026",
        "version": "2.1.285",
        "kind": "interactive",
        "entrypoint": "cli",
        "status": "busy",
        "updatedAt": started_ms + 1000,
    }
    record.update(overrides)
    return record


def _register(tmp_path: Path, session_id: str, **overrides) -> Path:
    path = _registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_registration(session_id, **overrides)), encoding="utf-8")
    return path


# ------------------------------------------------------------------
# 主路径：resume 之后
# ------------------------------------------------------------------


def test_resumed_conversation_wins_over_startup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    _register(tmp_path, RESUMED_ID)

    assert _base._cc_session_id() == RESUMED_ID
    assert _base.resolve_session_id() == RESUMED_ID


def test_in_place_rewrite_is_seen_even_with_identical_mtime_and_size(monkeypatch, tmp_path):
    """CC 原地改写登记（resume、/clear）。同一 mtime 刻度内的等长改写也要看得到。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    path = _register(tmp_path, STARTUP_ID)
    before = path.stat()
    assert _base._cc_session_id() == STARTUP_ID

    with open(path, "r+", encoding="utf-8") as fh:  # same inode, same length
        fh.write(json.dumps(_registration(RESUMED_ID)))
        fh.truncate()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)

    assert _base._cc_session_id() == RESUMED_ID


def test_atomic_replace_is_seen(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    path = _register(tmp_path, RESUMED_ID)
    assert _base._cc_session_id() == RESUMED_ID

    staged = path.with_suffix(".tmp")
    staged.write_text(json.dumps(_registration(CLEARED_ID)), encoding="utf-8")
    os.replace(staged, path)

    assert _base._cc_session_id() == CLEARED_ID


# ------------------------------------------------------------------
# 回落到环境变量：每条校验单独成对
# ------------------------------------------------------------------


def test_missing_registry_falls_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    assert not _registry_path(tmp_path).exists()
    assert _base._cc_session_id() == STARTUP_ID

    _register(tmp_path, RESUMED_ID)
    assert _base._cc_session_id() == RESUMED_ID


def test_registry_of_another_pid_is_not_used(monkeypatch, tmp_path):
    """文件名是父进程号、里面的 pid 却不是：不认。只按父进程号精确匹配。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    _register(tmp_path, RESUMED_ID, pid=os.getppid() + 1)
    assert _base._cc_session_id() == STARTUP_ID

    _register(tmp_path, RESUMED_ID)
    assert _base._cc_session_id() == RESUMED_ID


def test_other_processes_registries_are_never_consulted(monkeypatch, tmp_path):
    """别的 CC 进程的登记再新也不看（不按 cwd、不按最近修改时间猜）。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    other = _registry_path(tmp_path, os.getppid() + 7)
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(json.dumps(_registration(CLEARED_ID, pid=os.getppid() + 7)), encoding="utf-8")

    assert _base._cc_session_id() == STARTUP_ID

    _register(tmp_path, RESUMED_ID)
    assert _base._cc_session_id() == RESUMED_ID


@pytest.mark.parametrize(
    "raw",
    ['{"pid": 1, "sessionId": "39c7', "", "\x00\x01not json", "[]", '"a string"'],
    ids=["truncated", "empty", "binary", "json-list", "json-string"],
)
def test_corrupt_registry_falls_back_to_env(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    path = _registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    assert _base._cc_session_id() == STARTUP_ID

    _register(tmp_path, RESUMED_ID)
    assert _base._cc_session_id() == RESUMED_ID


@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "has space", "中文会话", "x" * 101, 42, None],
    ids=["empty", "blank", "space", "non-ascii", "too-long", "int", "null"],
)
def test_unusable_session_id_falls_back_to_env(monkeypatch, tmp_path, bad_id):
    """登记里的 id 必须能原样放进请求头（latin-1、无空白），否则整次请求会炸。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    _register(tmp_path, RESUMED_ID, sessionId=bad_id)
    assert _base._cc_session_id() == STARTUP_ID

    _register(tmp_path, RESUMED_ID)
    assert _base._cc_session_id() == RESUMED_ID


def test_stale_registry_left_by_a_dead_process_with_the_same_pid(monkeypatch, tmp_path):
    """同号旧文件：startedAt 早于父进程启动（超出容差）就不是父进程写的。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    parent_started_ms = psutil.Process(os.getppid()).create_time() * 1000
    _register(tmp_path, RESUMED_ID, startedAt=int(parent_started_ms - 3600_000))
    assert _base._cc_session_id() == STARTUP_ID

    _register(tmp_path, RESUMED_ID, startedAt=int(parent_started_ms + 2500))
    assert _base._cc_session_id() == RESUMED_ID


def test_registry_without_started_at_is_accepted(monkeypatch, tmp_path):
    """没有 startedAt 就只凭 pid 校验（环境变量门控已证明父进程是 CC）。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    record = _registration(RESUMED_ID)
    del record["startedAt"]
    path = _registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")

    assert _base._cc_session_id() == RESUMED_ID


def test_failed_read_keeps_the_last_good_id_instead_of_the_startup_id(monkeypatch, tmp_path):
    """撞上 CC 改写文件的瞬间（半截、暂缺）时，不退回启动时那个一次性 id。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    path = _register(tmp_path, RESUMED_ID)
    assert _base._cc_session_id() == RESUMED_ID

    path.write_text('{"pid": ', encoding="utf-8")
    assert _base._cc_session_id() == RESUMED_ID
    path.unlink()
    assert _base._cc_session_id() == RESUMED_ID

    _register(tmp_path, CLEARED_ID)
    assert _base._cc_session_id() == CLEARED_ID


def test_last_good_is_per_parent(monkeypatch, tmp_path):
    """上次的有效值只替同一个父进程顶班：被收养后（父进程号变了）不能沿用。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    _register(tmp_path, RESUMED_ID)
    assert _base._cc_session_id() == RESUMED_ID

    with patch.object(_base.os, "getppid", return_value=1):
        assert _base._cc_session_id() == STARTUP_ID


# ------------------------------------------------------------------
# Codex：不读登记，行为与改前一致
# ------------------------------------------------------------------


def test_stdio_server_without_cc_env_never_uses_the_registry(monkeypatch, tmp_path):
    """Codex 等宿主不注入 CLAUDE_CODE_SESSION_ID：父进程号下哪怕有登记也不读。"""
    _register(tmp_path, RESUMED_ID)

    assert _base._cc_session_id() == ""
    assert _base.resolve_session_id() is None
    assert infra._caller_host() == "codex"

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    assert _base._cc_session_id() == RESUMED_ID
    assert infra._caller_host() == "cc"


def test_http_mcp_scope_never_uses_env_or_registry(monkeypatch, tmp_path):
    """HTTP MCP（Codex）的请求上下文里没有 CC 会话 id，就是没有。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    _register(tmp_path, RESUMED_ID)

    token = _base._http_request_context.set({"project_id": "p", "mcp_session_id": "m-1"})
    try:
        assert _base._cc_session_id() == ""
        assert _base.resolve_session_id() is None
    finally:
        _base._http_request_context.reset(token)

    assert _base._cc_session_id() == RESUMED_ID


# ------------------------------------------------------------------
# 依赖会话 id 的 MCP 行为
# ------------------------------------------------------------------


def _team(team_id: str, owner: str, created_at: str) -> dict:
    return {
        "id": team_id,
        "name": f"session-{owner[:8]}",
        "status": "active",
        "project_id": "proj-1",
        "config": {"kind": "session", "owner_session_id": owner},
        "created_at": created_at,
    }


def test_team_resolution_follows_the_resumed_conversation(monkeypatch, tmp_path):
    """resume 起的空壳容器队更新，但当前团队必须是被续上的那场对话的队。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    monkeypatch.setattr(_base, "_session_project_id", "proj-1")
    rows = {
        "success": True,
        "data": [
            _team("shell-team", STARTUP_ID, "2026-09-30T05:00:00"),
            _team("real-team", RESUMED_ID, "2026-09-03T02:28:38"),
        ],
    }
    _register(tmp_path, RESUMED_ID)
    with patch.object(_base, "_api_call", return_value=rows):
        assert _base._resolve_team_id("") == "real-team"


class _HeaderSink(BaseHTTPRequestHandler):
    seen: list[str | None] = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).seen.append(self.headers.get("X-CC-Session-Id"))  # case-insensitive lookup
        body = b'{"success": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_api_call_sends_the_resumed_conversation_as_session_header(monkeypatch, tmp_path):
    """服务端按 X-CC-Session-Id 解析记账作者的域：头里必须是被续上的对话 id。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    _register(tmp_path, RESUMED_ID)
    _HeaderSink.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HeaderSink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("AITEAM_API_URL", f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        assert _base._api_call("GET", "/api/ping") == {"success": True}
    finally:
        server.shutdown()
        server.server_close()

    assert _HeaderSink.seen == [RESUMED_ID]
