"""Tests for MCP Server FastAPI auto-start helpers."""

from __future__ import annotations

import socket
from unittest.mock import patch

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
@patch("aiteam.mcp._autostart._get_running_api_version", return_value="0.8.0")
@patch("aiteam.mcp._autostart.subprocess.Popen")
def test_ensure_api_skips_when_running(mock_popen, mock_version, mock_port):
    """Port already occupied with matching version — subprocess must not be spawned."""
    _ensure_api_running()
    mock_popen.assert_not_called()
