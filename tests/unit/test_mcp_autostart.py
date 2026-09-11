"""Tests for MCP Server FastAPI auto-start helpers."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

import aiteam as _aiteam_pkg
from aiteam.mcp import _autostart
from aiteam.mcp._autostart import _ensure_api_running, _is_port_open


def test_is_port_open_returns_false():
    """未监听的端口应返回 False。"""
    # 使用一个极不可能被占用的高端口
    assert _is_port_open("127.0.0.1", 59999) is False


def test_is_port_open_returns_true():
    """已监听的端口应返回 True。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert _is_port_open("127.0.0.1", port) is True
    finally:
        srv.close()


@patch("aiteam.mcp._autostart._is_port_open", return_value=True)
@patch("aiteam.mcp._autostart._is_api_healthy_on_port", return_value=True)
@patch(
    "aiteam.mcp._autostart._get_running_api_version_on_port",
    # 必须跟随真实包版本——曾硬编码 "1.3.4"，包升级后被判"版本过时"
    # 走进 kill 占用者路径（fuser/lsof 的 Popen），断言虚假失败
    return_value=_aiteam_pkg.__version__,
)
@patch("aiteam.mcp._autostart.subprocess.Popen")
# 隔离真实运行时文件：_debug_log 写 ~/.claude/data/ai-team-os/mcp-debug.log，
# _get_api_port 读真实 api_port.txt——单测不得污染/依赖它们
@patch("aiteam.mcp._autostart._get_api_port", return_value=8000)
@patch("aiteam.mcp._autostart._debug_log")
def test_ensure_api_skips_when_running(
    mock_debug_log, mock_get_port, mock_popen, mock_version_on_port, mock_healthy, mock_port
):
    """Port already occupied with matching version — subprocess must not be spawned."""
    with patch("aiteam.mcp._autostart._reconcile_api_pid") as reconcile:
        _ensure_api_running()
        reconcile.assert_called_once_with(8000)
    mock_popen.assert_not_called()


@pytest.mark.parametrize("os_name,detached", [("posix", True), ("nt", False)])
def test_autostart_detaches_only_posix_sessions(tmp_path, monkeypatch, os_name, detached):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    monkeypatch.setattr(_autostart, "_api_process", None)
    monkeypatch.setattr(_autostart, "_PID_FILE", str(tmp_path / "api.pid"))
    monkeypatch.setattr(_autostart, "_DEBUG_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(_autostart, "_API_STDERR_LOG", str(tmp_path / "stderr.log"))
    monkeypatch.setattr(_autostart, "_DEFAULT_PORT", 8765)
    with (
        patch.object(_autostart, "_debug_log"),
        patch.object(_autostart, "_read_pid_file", return_value=None),
        patch.object(_autostart, "_is_port_open", return_value=False),
        patch.object(_autostart, "_is_api_healthy_on_port", return_value=True),
        patch.object(_autostart, "_write_pid_file") as write_pid,
        patch.object(_autostart, "_save_api_port") as save_port,
        patch.object(_autostart.atexit, "register"),
        patch.object(_autostart.time, "sleep"),
        patch.object(_autostart.os, "name", os_name),
        patch.object(_autostart.subprocess, "Popen", return_value=process) as spawn,
    ):
        _autostart._ensure_api_running_locked(_aiteam_pkg.__version__)

    assert spawn.call_args.kwargs["start_new_session"] is detached
    assert spawn.call_args.kwargs.get("creationflags", 0) == 0
    assert spawn.call_args.args[0][-2:] == ["8765", "--factory"]
    write_pid.assert_called_once_with(12345, lock_held=True)
    save_port.assert_called_once_with(8765)
