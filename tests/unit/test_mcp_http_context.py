"""HTTP scope is request-local while stdio keeps its existing environment rules."""

from __future__ import annotations

import asyncio
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anyio
import pytest
from fastmcp import FastMCP

from aiteam.mcp import _base
from aiteam.mcp import _http_context as http_context
from aiteam.mcp import http_headers as helper
from aiteam.mcp._http_context import _project_for_directory
from aiteam.mcp.tools import _HTTPToolRegistration, register_all


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
    assert "scripts/codex_adapter.py start --api-url http://127.0.0.1:8000" in output.err
    assert "From the AI Team OS repository" in output.err
    assert "PYTHONPATH=" not in output.err  # A copied helper cannot infer the repository location.


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


def test_helper_healthy_connection_does_not_spawn_runtime(monkeypatch, capsys):
    monkeypatch.setattr(helper.sys, "argv", ["http_headers.py", "--api-url", "http://127.0.0.1:8000",
                                            "--runtime-script", "/unused/hot-path.py",
                                            "--runtime-dir", "/unused/runtime"])
    monkeypatch.setattr(helper, "_ready", lambda *args: True)
    spawn = Mock(side_effect=AssertionError("healthy calls must not spawn"))
    monkeypatch.setattr(helper.subprocess, "run", spawn)
    assert helper.main() == 0
    assert json.loads(capsys.readouterr().out) == helper.connection_headers()
    spawn.assert_not_called()


async def test_http_thread_adapter_preserves_every_tool_schema(monkeypatch):
    monkeypatch.delenv("AITEAM_READONLY", raising=False)
    monkeypatch.delenv("AITEAM_TOOLSETS", raising=False)
    baseline, adapted = FastMCP("baseline"), FastMCP("adapted")
    register_all(baseline)
    register_all(adapted, isolate_http_threads=True)
    expected = {tool.name: tool.to_mcp_tool().model_dump() for tool in await baseline.list_tools()}
    actual = {tool.name: tool.to_mcp_tool().model_dump() for tool in await adapted.list_tools()}
    assert actual == expected
    assert len(actual) > 100


@pytest.mark.parametrize("http", [False, True], ids=["stdio-default-pool", "http-separate-pool"])
async def test_sync_tool_preserves_request_context_and_stdio_pool(http):
    default_limiter = anyio.to_thread.current_default_thread_limiter()
    event_loop_thread = threading.get_ident()
    server = FastMCP("thread-probe")
    registration = _HTTPToolRegistration(server)

    @registration.tool()
    def probe(label: str, suffix: str = "default") -> dict:
        assert threading.get_ident() != event_loop_thread
        assert default_limiter.borrowed_tokens == (0 if http else 1)
        return {"label": label + suffix, "project": _base._current_project_id()}

    scope = {"project_id": "http-only"} if http else None
    token = _base._http_request_context.set(scope)
    try:
        tool = await server.get_tool("probe")
        result = await tool.run({"label": "context-"})
        assert result.structured_content == {
            "label": "context-default", "project": "http-only" if http else _base._session_project_id,
        }
    finally:
        _base._http_request_context.reset(token)
    assert default_limiter.borrowed_tokens == 0
    assert _base._http_request_context.get() is None


async def test_http_context_and_tool_run_while_rest_dependency_pool_is_exhausted(monkeypatch):
    request = SimpleNamespace(
        scope={"server": ("127.0.0.1", 8123)}, headers={"x-aiteam-project-dir": "/work"},
    )
    monkeypatch.setattr(http_context, "get_http_request", lambda: request)

    def projects(method, path):
        assert (method, path) == ("GET", "/api/projects")
        assert _base._get_api_url() == "http://127.0.0.1:8123"
        return {"data": [{"id": "verified", "root_path": "/work"}]}

    monkeypatch.setattr(_base, "_api_call", projects)
    server = FastMCP("pool-probe")
    registration = _HTTPToolRegistration(server)

    @registration.tool()
    def probe() -> dict:
        return {"project": _base._current_project_id(), "cwd": _base._current_cwd()}

    async def downstream(context):
        tool = await server.get_tool("probe")
        return await tool.run({})

    # Occupy all default permits without starting blocking worker threads. Both
    # the context lookup AND synchronous tool must still reach their own pool.
    limiter = anyio.to_thread.current_default_thread_limiter()
    borrowers = [object() for _ in range(int(limiter.total_tokens))]
    for borrower in borrowers:
        await limiter.acquire_on_behalf_of(borrower)
    try:
        result = await asyncio.wait_for(http_context.HTTPProjectContext().on_call_tool(None, downstream), 5)
        assert result.structured_content == {"project": "verified", "cwd": "/work"}
    finally:
        for borrower in borrowers:
            limiter.release_on_behalf_of(borrower)
    assert _base._http_request_context.get() is None


async def test_http_client_pool_is_bounded_and_leaves_both_rest_pools_available():
    loop = asyncio.get_running_loop()
    lock = threading.Lock()
    release = threading.Event()
    full = asyncio.Event()
    active = peak = completed = 0

    def client_wait():
        nonlocal active, peak, completed
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 32:
                loop.call_soon_threadsafe(full.set)
        try:
            assert release.wait(10), "Test did not release the HTTP clients"
        finally:
            with lock:
                active -= 1
                completed += 1

    tasks = [asyncio.create_task(_base._run_http_sync(client_wait)) for _ in range(48)]
    try:
        await asyncio.wait_for(full.wait(), 5)
        # FastAPI dependencies and its occasional asyncio.to_thread handlers
        # must remain runnable while every HTTP-client permit is occupied.
        assert await asyncio.wait_for(anyio.to_thread.run_sync(lambda: "dependency"), 5) == "dependency"
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "handler"), 5) == "handler"
        with lock:
            assert active == peak == 32
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert completed == 48 and active == 0 and peak == 32
