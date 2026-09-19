"""HTTP scope is request-local while stdio keeps its existing environment rules."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from aiteam.mcp import _base
from aiteam.mcp import _http_context as http_context
from aiteam.mcp import http_headers as helper
from aiteam.mcp._http_context import _project_for_directory


def test_longest_directory_match_and_unknown_path():
    projects = [{"id": "outer", "root_path": "/work"}, {"id": "inner", "root_path": "/work/repo"}]
    assert _project_for_directory("/work/repo/.worktrees/feature", projects) == "inner"
    assert _project_for_directory("/workspace", projects) == ""


def test_ambiguous_project_root_is_rejected():
    with pytest.raises(ValueError, match="multiple projects"):
        _project_for_directory("/work/repo", [{"id": "one", "root_path": "/work"},
                                               {"id": "two", "root_path": "/work"}])


@pytest.mark.asyncio
async def test_request_context_never_changes_stdio_globals(monkeypatch):
    monkeypatch.setattr(_base, "_session_project_id", "stdio-project")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "stdio-session")

    async def scoped(project_id):
        token = _base._http_request_context.set({"project_id": project_id})
        try:
            await asyncio.sleep(0)
            assert _base._resolve_project_id("") == project_id
            assert _base._cc_session_id() == ""
            assert _base._resolve_project_id("explicit") == "explicit"
        finally:
            _base._http_request_context.reset(token)

    await asyncio.gather(scoped("http-one"), scoped("http-two"))
    assert _base._resolve_project_id("") == "stdio-project"
    assert _base._cc_session_id() == "stdio-session"


def test_http_api_address_overrides_environment_and_requires_scope(monkeypatch):
    monkeypatch.setenv("AITEAM_API_URL", "http://legacy-invalid.example")
    token = _base._http_request_context.set({"api_url": "http://127.0.0.1:8123"})
    try:
        assert _base._get_api_url() == "http://127.0.0.1:8123"
        _base._http_request_context.set({})
        with pytest.raises(ValueError, match="no verified"):
            _base._get_api_url()
    finally:
        _base._http_request_context.reset(token)
    assert _base._get_api_url() == "http://legacy-invalid.example"


@pytest.mark.asyncio
async def test_missing_asgi_server_is_rejected_before_api_call(monkeypatch):
    request = SimpleNamespace(scope={}, headers={"host": "127.0.0.1:8000", "x-aiteam-project-dir": "/work"})
    monkeypatch.setattr(http_context, "get_http_request", lambda: request)
    api_call = Mock()
    monkeypatch.setattr(_base, "_api_call", api_call)
    downstream = AsyncMock()
    with pytest.raises(ValueError, match="ASGI server"):
        await http_context.HTTPProjectContext().on_call_tool(None, downstream)
    api_call.assert_not_called()
    downstream.assert_not_awaited()
    assert _base._http_request_context.get() is None


@pytest.mark.parametrize("readiness", [None, {"status": "ok", "version": "1.13.0"}])
def test_helper_fails_without_verified_api_and_emits_no_scope(monkeypatch, capsys, readiness):
    monkeypatch.setattr(helper.sys, "argv", ["http_headers.py", "--api-url", "http://127.0.0.1:8000"])
    opener = Mock()
    if readiness is None:
        opener.open.side_effect = OSError("offline")
    else:
        opener.open.return_value = io.StringIO(json.dumps(readiness))
    monkeypatch.setattr(helper.urllib.request, "build_opener", lambda *args: opener)
    assert helper.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "-m uvicorn aiteam.api.app:create_app" in output.err
    assert "--host 127.0.0.1 --port 8000" in output.err
    assert "PYTHONPATH=" in output.err


def test_helper_emits_cwd_but_does_not_claim_inherited_thread_identity(monkeypatch, capsys):
    monkeypatch.setattr(helper.sys, "argv", ["http_headers.py", "--api-url", "http://127.0.0.1:8000"])
    monkeypatch.setenv("CODEX_THREAD_ID", "not-the-new-thread")
    opener = Mock()
    opener.open.return_value = io.StringIO(json.dumps({"status": "ok", "project_context": "connection-cwd-v1"}))
    monkeypatch.setattr(helper.urllib.request, "build_opener", lambda *args: opener)
    assert helper.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == helper.connection_headers()
    assert "not-the-new-thread" not in output.out
