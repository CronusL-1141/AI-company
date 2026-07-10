"""Unit tests for the standardized API restart flow.

Covers the os_restart_api MCP tool (busy-agent guard, port-pin guard,
dead-before-spawn guard, happy path) and the /api/system/shutdown helpers.

All process/HTTP/spawn interactions are mocked — these tests NEVER touch a
live API or spawn a real uvicorn process.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import aiteam.mcp.tools.infra as infra

# ---------------------------------------------------------------------------
# Tool capture helper (mirrors tests/unit/mcp/test_ecosystem_tools.py)
# ---------------------------------------------------------------------------


class _ToolCapture:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


_capture = _ToolCapture()
infra.register(_capture)
_os_restart_api = _capture.tools["os_restart_api"]


def _health(version: str | None) -> dict | None:
    return {"status": "ok", "version": version} if version else None


def _ticking_clock(step: float = 10.0):
    """Fake time.monotonic that ADVANCES *step* seconds on every call.

    Never assume how many times the implementation reads the clock — a
    call-count-based fake here once produced an infinite tight loop (the
    deadline was re-read after the mocked jump) that ate 32GB of RAM via
    mock call recording and froze the machine. A monotonically advancing
    clock guarantees every deadline loop terminates within a few iterations
    regardless of where the implementation samples the time.
    """
    state = {"t": -step}

    def fake_monotonic() -> float:
        state["t"] += step
        return state["t"]

    return fake_monotonic


# ---------------------------------------------------------------------------
# Guard 1: busy agents
# ---------------------------------------------------------------------------


def test_restart_refuses_when_agents_busy():
    """Active team with a busy agent → refuse, no shutdown POST."""
    teams = {"data": [{"id": "t1", "status": "active"}]}
    agents = {"data": [{"name": "a1", "status": "busy"}]}

    def fake_get(path, port, timeout=2.0):
        if path == "/api/health":
            return _health("1.6.0")
        if path == "/api/teams":
            return teams
        if path.startswith("/api/teams/t1/agents"):
            return agents
        return None

    with (
        patch.object(infra, "_restart_local_get", side_effect=fake_get),
        patch.object(infra, "_restart_local_post") as mock_post,
    ):
        result = _os_restart_api(force=False)

    assert result["success"] is False
    assert result["error"] == "busy_agents"
    assert "1" in result["detail"]
    mock_post.assert_not_called()  # never requested shutdown


def test_restart_force_bypasses_busy_guard():
    """force=True restarts even with a busy agent (happy path under force)."""
    teams = {"data": [{"id": "t1", "status": "active"}]}
    agents = {"data": [{"name": "a1", "status": "busy"}]}
    health_seq = iter([_health("1.6.0"), _health("1.6.1")])

    def fake_get(path, port, timeout=2.0):
        if path == "/api/health":
            return next(health_seq)
        if path == "/api/teams":
            return teams
        if path.startswith("/api/teams/t1/agents"):
            return agents
        return None

    with (
        patch.object(infra, "_restart_local_get", side_effect=fake_get),
        patch.object(infra, "_restart_local_post", return_value={"success": True, "pid": 111}),
        patch.object(infra, "_restart_pid_alive", return_value=False),
        patch.object(infra, "_restart_spawn_on_port", return_value={"success": True, "new_pid": 222}),
        patch("aiteam.mcp._autostart._get_api_port", return_value=8000),
        patch("aiteam.mcp._autostart._read_pid_file", return_value=111),
        patch("aiteam.mcp._autostart._is_port_open", return_value=False),
    ):
        result = _os_restart_api(force=True)

    assert result["success"] is True
    assert result["old_version"] == "1.6.0"
    assert result["new_version"] == "1.6.1"
    assert result["old_pid"] == 111
    assert result["new_pid"] == 222
    assert "elapsed_ms" in result


# ---------------------------------------------------------------------------
# Guard 3: shutdown must succeed and old process must die before spawn
# ---------------------------------------------------------------------------


def test_restart_aborts_when_shutdown_fails():
    """shutdown POST returns failure → abort, never spawn."""
    def fake_get(path, port, timeout=2.0):
        if path == "/api/health":
            return _health("1.6.0")
        if path == "/api/teams":
            return {"data": []}
        return None

    with (
        patch.object(infra, "_restart_local_get", side_effect=fake_get),
        patch.object(infra, "_restart_local_post", return_value=None),
        patch.object(infra, "_restart_spawn_on_port") as mock_spawn,
        patch("aiteam.mcp._autostart._get_api_port", return_value=8000),
        patch("aiteam.mcp._autostart._read_pid_file", return_value=111),
    ):
        result = _os_restart_api(force=True)

    assert result["success"] is False
    assert result["error"] == "shutdown_failed"
    mock_spawn.assert_not_called()


def test_restart_aborts_when_old_process_wont_die():
    """Old process stays alive past the 10s window → abort, never spawn."""
    def fake_get(path, port, timeout=2.0):
        if path == "/api/health":
            return _health("1.6.0")
        if path == "/api/teams":
            return {"data": []}
        return None

    with (
        patch.object(infra, "_restart_local_get", side_effect=fake_get),
        patch.object(infra, "_restart_local_post", return_value={"success": True}),
        patch.object(infra, "_restart_pid_alive", return_value=True),  # never dies
        patch.object(infra, "_restart_spawn_on_port") as mock_spawn,
        patch("aiteam.mcp.tools.infra.time.monotonic", side_effect=_ticking_clock()),
        patch("aiteam.mcp.tools.infra.time.sleep"),
        patch("aiteam.mcp._autostart._get_api_port", return_value=8000),
        patch("aiteam.mcp._autostart._read_pid_file", return_value=111),
        patch("aiteam.mcp._autostart._is_port_open", return_value=True),
    ):
        result = _os_restart_api(force=True)

    assert result["success"] is False
    assert result["error"] == "shutdown_timeout"
    mock_spawn.assert_not_called()


# ---------------------------------------------------------------------------
# Guard 2: port pinning when API already down
# ---------------------------------------------------------------------------


def test_restart_aborts_when_port_held_by_unrelated_process():
    """API down but original port occupied by unknown process → abort, never spawn."""
    with (
        patch.object(infra, "_restart_local_get", return_value=None),  # API down
        patch.object(infra, "_restart_spawn_on_port") as mock_spawn,
        patch("aiteam.mcp._autostart._get_api_port", return_value=8000),
        patch("aiteam.mcp._autostart._read_pid_file", return_value=None),
        patch("aiteam.mcp._autostart._is_port_open", return_value=True),  # occupied
    ):
        result = _os_restart_api(force=False)

    assert result["success"] is False
    assert result["error"] == "port_occupied"
    mock_spawn.assert_not_called()


def test_restart_starts_when_api_already_down():
    """API down + port free → behaves as a plain start on the configured port."""
    health_seq = iter([None, _health("1.6.1")])  # first probe down, then healthy

    def fake_get(path, port, timeout=2.0):
        if path == "/api/health":
            return next(health_seq)
        return None

    with (
        patch.object(infra, "_restart_local_get", side_effect=fake_get),
        patch.object(infra, "_restart_spawn_on_port", return_value={"success": True, "new_pid": 333}),
        patch("aiteam.mcp._autostart._get_api_port", return_value=8000),
        patch("aiteam.mcp._autostart._read_pid_file", return_value=None),
        patch("aiteam.mcp._autostart._is_port_open", return_value=False),  # free
    ):
        result = _os_restart_api(force=False)

    assert result["success"] is True
    assert result["old_version"] is None  # was down
    assert result["new_version"] == "1.6.1"
    assert result["new_pid"] == 333


# ---------------------------------------------------------------------------
# Health timeout after spawn
# ---------------------------------------------------------------------------


def test_restart_reports_health_timeout():
    """New process never becomes healthy → health_timeout with new_pid surfaced."""

    def fake_get(path, port, timeout=2.0):
        if path == "/api/health":
            return None  # never healthy after spawn
        return None

    with (
        patch.object(infra, "_restart_local_get", side_effect=fake_get),
        patch.object(infra, "_restart_spawn_on_port", return_value={"success": True, "new_pid": 444}),
        patch("aiteam.mcp.tools.infra.time.monotonic", side_effect=_ticking_clock()),
        patch("aiteam.mcp.tools.infra.time.sleep"),
        patch("aiteam.mcp._autostart._get_api_port", return_value=8000),
        patch("aiteam.mcp._autostart._read_pid_file", return_value=None),
        patch("aiteam.mcp._autostart._is_port_open", return_value=False),
    ):
        result = _os_restart_api(force=False)

    assert result["success"] is False
    assert result["error"] == "health_timeout"
    assert result["new_pid"] == 444


# ---------------------------------------------------------------------------
# Helper: _restart_pid_alive
# ---------------------------------------------------------------------------


def test_pid_alive_true_via_psutil():
    """僵尸判定改造后：psutil 路径按 Process(pid).status() != ZOMBIE 判活。"""
    fake_psutil = MagicMock()
    fake_psutil.STATUS_ZOMBIE = "zombie"
    fake_psutil.Process.return_value.status.return_value = "running"
    with patch.dict("sys.modules", {"psutil": fake_psutil}):
        assert infra._restart_pid_alive(123) is True
    fake_psutil.Process.assert_called_once_with(123)


def test_pid_alive_false_for_dead_pid():
    """Without psutil, a dead PID via os.kill raising → False."""
    with (
        patch.dict("sys.modules", {"psutil": None}),
        patch.object(infra.os, "kill", side_effect=ProcessLookupError),
    ):
        assert infra._restart_pid_alive(999999) is False


# ---------------------------------------------------------------------------
# Helper: _restart_spawn_on_port records PID/port and never drifts
# ---------------------------------------------------------------------------


def test_spawn_on_port_writes_pid_and_port():
    autostart = MagicMock()
    fake_proc = MagicMock()
    fake_proc.pid = 555
    with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
        result = infra._restart_spawn_on_port(autostart, 8000)

    assert result == {"success": True, "new_pid": 555}
    autostart._write_pid_file.assert_called_once_with(555)
    autostart._save_api_port.assert_called_once_with(8000)
    # spawned on the supplied port, not a random one
    argv = mock_popen.call_args[0][0]
    assert "--port" in argv and argv[argv.index("--port") + 1] == "8000"


def test_spawn_on_port_handles_popen_failure():
    autostart = MagicMock()
    with patch("subprocess.Popen", side_effect=OSError("boom")):
        result = infra._restart_spawn_on_port(autostart, 8000)
    assert result["success"] is False
    assert result["error"] == "spawn_failed"


# ---------------------------------------------------------------------------
# Shutdown endpoint helpers (system route)
# ---------------------------------------------------------------------------


def test_wal_checkpoint_swallows_errors():
    """WAL checkpoint must never raise even if sqlite3.connect fails."""
    from aiteam.api.routes import system

    with patch("sqlite3.connect", side_effect=OSError("locked")):
        # Must not raise
        system._wal_checkpoint_best_effort()


def test_shutdown_endpoint_returns_pid_and_schedules_exit():
    """POST handler returns success+pid and schedules a delayed exit task."""
    import asyncio

    from aiteam.api.routes import system

    async def run():
        with (
            patch.object(system.asyncio, "create_task") as mock_task,
            patch.object(system.os, "getpid", return_value=4242),
        ):
            resp = await system.shutdown()
        return resp, mock_task

    resp, mock_task = asyncio.run(run())
    assert resp == {"success": True, "message": "shutting down", "pid": 4242}
    mock_task.assert_called_once()  # _delayed_exit scheduled, never awaited here


def test_restart_pid_alive_zombie_is_dead(monkeypatch):
    """defunct 僵尸必须视为已死——优雅停机成功后子进程滞留僵尸时，
    旧实现 pid_exists 判活导致 shutdown_timeout 误报（2026-07-06 巡检实录）。"""
    import sys as _sys
    import types as _types

    fake = _types.ModuleType("psutil")

    class _NoSuchError(Exception):
        pass

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def status(self):
            return "zombie"

    fake.NoSuchProcess = _NoSuchError
    fake.STATUS_ZOMBIE = "zombie"
    fake.Process = _Proc
    monkeypatch.setitem(_sys.modules, "psutil", fake)
    assert infra._restart_pid_alive(12345) is False
