"""Tests for MCP Server FastAPI auto-start helpers."""

from __future__ import annotations

import socket
from unittest.mock import patch

import aiteam as _aiteam_pkg
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
    _ensure_api_running()
    mock_popen.assert_not_called()
