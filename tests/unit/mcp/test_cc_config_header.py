"""The stdio MCP server names its session's Claude Code config folder on every API call (design §4.2)."""

from __future__ import annotations

import io
import json
import urllib.parse

import pytest

from aiteam.mcp import _base


@pytest.fixture()
def sent(monkeypatch):
    """Capture the request _api_call builds instead of sending it."""
    requests = []

    def urlopen(request, timeout=None):
        requests.append(request)
        return io.BytesIO(json.dumps({"success": True}).encode())

    monkeypatch.setattr(_base.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(_base, "response_snapshot", lambda response: {})
    return requests


@pytest.mark.parametrize("session_id", ["", "11111111-2222-3333-4444-555555555555"],
                         ids=["no-session-id", "session-id"])
def test_a_stdio_call_names_the_config_folder_with_or_without_a_session_id(sent, monkeypatch, tmp_path, session_id):
    folder = tmp_path / "配置 目录"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    if session_id:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)

    _base._api_call("GET", "/api/health")

    [request] = sent
    assert urllib.parse.unquote(request.get_header("X-cc-config-dir")) == str(folder)
