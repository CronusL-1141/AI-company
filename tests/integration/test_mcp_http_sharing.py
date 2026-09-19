"""Independent HTTP MCP sessions share one API without sharing project scope."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from .test_mcp_process_groups import ProcessIdentity, _isolated_mcp, _stop_owned, _wait_for

ROOT = Path(__file__).resolve().parents[2]


def _rpc_result(response: httpx.Response) -> dict:
    response.raise_for_status()
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    messages = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
    assert len(messages) == 1, messages
    return messages[0]


async def _connect(client: httpx.AsyncClient, directory: Path | None):
    client.headers.update({"Accept": "application/json, text/event-stream"})
    if directory is not None:
        client.headers["X-Aiteam-Project-Dir"] = quote(str(directory), safe="/:.-_\\")
    response = await client.post("/mcp/", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "http-sharing-test", "version": "1"}},
    })
    assert _rpc_result(response)["result"]["serverInfo"]["name"] == "ai-team-os"
    client.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
    response = await client.post("/mcp/", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202


async def _call(client: httpx.AsyncClient, request_id: int, tool: str, arguments: dict | None = None):
    return _rpc_result(await client.post("/mcp/", json={
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }))


def _project(port: int, directory: Path, name: str) -> str:
    directory.mkdir()
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
        response = client.post("/api/projects", json={"name": name, "root_path": str(directory)})
        response.raise_for_status()
        return response.json()["data"]["id"]


def test_shared_http_sessions_keep_scope_and_cancel_independently(tmp_path):
    with _isolated_mcp(tmp_path, "autostart") as runtime:
        _, _, api_identity, port, _ = runtime
        directory_a, directory_b = tmp_path / "project-a", tmp_path / "project-b"
        project_a = _project(port, directory_a, "HTTP A")
        project_b = _project(port, directory_b, "HTTP B")

        async def exercise():
            async with (
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15) as first,
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15) as second,
            ):
                await _connect(first, directory_a)
                await _connect(second, directory_b)
                assert first.headers["Mcp-Session-Id"] != second.headers["Mcp-Session-Id"]
                a, b = await asyncio.gather(_call(first, 2, "context_resolve"), _call(second, 2, "context_resolve"))
                assert a["result"]["structuredContent"]["project"]["id"] == project_a
                assert b["result"]["structuredContent"]["project"]["id"] == project_b
                arguments = {"channel": "team:http-sharing", "reader": "receiver", "sender": "peer",
                             "since": "2026-01-01T00:00:00Z", "timeout_seconds": 45}
                waiting_a = asyncio.create_task(_call(first, 3, "channel_wait", arguments))
                waiting_b = asyncio.create_task(_call(second, 3, "channel_wait", arguments))
                try:
                    log = tmp_path / "home/.claude/data/ai-team-os/debug.log"
                    async with asyncio.timeout(5):
                        while sum("/inbox?" in line and " 200" in line for line in log.read_text().splitlines()) < 2:
                            await asyncio.sleep(0.02)
                    assert not waiting_a.done() and not waiting_b.done()
                    cancelled = await first.post("/mcp/", json={
                        "jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": 3, "reason": "Cancel only connection A"},
                    })
                    assert cancelled.status_code == 202
                    assert "cancelled" in (await asyncio.wait_for(waiting_a, 3))["error"]["message"]
                    assert not waiting_b.done()
                    resumed_a = await _call(first, 4, "context_resolve")
                    assert resumed_a["result"]["structuredContent"]["project"]["id"] == project_a
                    assert (await first.delete("/mcp/")).status_code == 200
                    assert not waiting_b.done()
                    response = await second.post("/api/channels/team:http-sharing/messages", json={
                        "sender": "peer", "content": "Connection B remains live", "mentions": ["receiver"],
                        "project_id": project_b,
                    })
                    response.raise_for_status()
                    delivered = (await asyncio.wait_for(waiting_b, 3))["result"]["structuredContent"]
                    assert delivered["success"]
                    assert delivered["data"]["messages"][0]["project_id"] == project_b
                    resumed_b = await _call(second, 4, "context_resolve")
                    assert resumed_b["result"]["structuredContent"]["project"]["id"] == project_b
                    assert (await second.delete("/mcp/")).status_code == 200
                finally:
                    for task in (waiting_a, waiting_b):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(waiting_a, waiting_b, return_exceptions=True)

        asyncio.run(exercise())
        assert api_identity.live() is not None and api_identity.live().children() == []


@pytest.mark.parametrize("unregistered", [False, True])
def test_http_missing_verified_scope_fails_closed(tmp_path, unregistered):
    with _isolated_mcp(tmp_path, "autostart") as runtime:
        _, _, _, port, _ = runtime
        _project(port, tmp_path / "registered", "Must not leak")

        async def exercise():
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                await _connect(client, tmp_path / "unknown" if unregistered else None)
                result = (await _call(client, 2, "context_resolve"))["result"]
                assert result["isError"] is True
                assert "structuredContent" not in result
                assert "Must not leak" not in json.dumps(result)
                assert (await client.delete("/mcp/")).status_code == 200

        asyncio.run(exercise())


@contextmanager
def _native_host(tmp_path: Path, api_identity: ProcessIdentity, port: int, binary: str):
    home = tmp_path / "native-codex"
    home.mkdir()
    host_cwd = tmp_path / "native-host-cwd"
    host_cwd.mkdir()
    env = api_identity.live().environ()
    env["CODEX_HOME"] = str(home)
    helper = shlex.join([sys.executable, str(ROOT / "src/aiteam/mcp/http_headers.py"),
                        "--api-url", f"http://127.0.0.1:{port}"])
    with (tmp_path / "native-codex.stderr.log").open("wb") as stderr:
        process = subprocess.Popen([
            binary, "app-server", "-c", f'mcp_servers.aiteam.url="http://127.0.0.1:{port}/mcp/"',
            "-c", "mcp_servers.aiteam.http_headers_helper=" + json.dumps(helper),
            "-c", "mcp_servers.aiteam.startup_timeout_sec=15",
            # Fresh native hosts otherwise fetch an unrelated plugin marketplace
            # and prewarm the default provider during thread/start.
            "-c", "features.plugins=false",
            "-c", 'model_provider="isolated-mcp-test"',
            "-c", 'model_providers.isolated-mcp-test.name="Isolated MCP test"',
            "-c", f'model_providers.isolated-mcp-test.base_url="http://127.0.0.1:{port}/no-model"',
            "-c", 'model_providers.isolated-mcp-test.wire_api="responses"',
            "-c", "model_providers.isolated-mcp-test.requires_openai_auth=false",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            cwd=host_cwd, env=env, start_new_session=True)
    identity = None
    messages: queue.Queue[dict] = queue.Queue()

    def receive():
        for line in process.stdout:
            messages.put(json.loads(line))

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    sequence = 0

    def rpc(method: str, params: dict):
        nonlocal sequence
        sequence += 1
        process.stdin.write((json.dumps({"id": sequence, "method": method, "params": params}) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if result.get("id") == sequence:
                assert "error" not in result, result
                return result["result"]
        pytest.fail(f"Native app-server did not answer {method}")

    try:
        identity = ProcessIdentity.capture(process.pid, host_cwd)
        rpc("initialize", {"clientInfo": {"name": "aiteam-http-sharing-test", "version": "1"},
                           "capabilities": {"experimentalApi": True}})
        process.stdin.write(b'{"method":"initialized","params":{}}\n')
        process.stdin.flush()
        yield identity, rpc
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _stop_owned(identity)
            process.wait(timeout=5)
        reader.join(timeout=3)
        process.stdout.close()


def test_native_http_threads_have_distinct_cwd_and_no_resident_python(tmp_path):
    from aiteam.mcp.server import mcp

    binary = os.environ.get("AITEAM_TEST_CODEX_BINARY")
    if not binary:
        pytest.skip("Set AITEAM_TEST_CODEX_BINARY for an isolated native app-server contract check")
    expected_tools = {tool.name for tool in asyncio.run(mcp.list_tools())}
    with _isolated_mcp(tmp_path, "autostart") as runtime:
        _, _, api_identity, port, _ = runtime
        directory_a, directory_b = tmp_path / "native-project-a", tmp_path / "native-project-b"
        expected = [(_project(port, directory_a, "Native A"), directory_a),
                    (_project(port, directory_b, "Native B"), directory_b)]
        with _native_host(tmp_path, api_identity, port, binary) as (host, rpc):
            for project_id, directory in expected:
                thread_id = rpc("thread/start", {"cwd": str(directory), "ephemeral": True})["thread"]["id"]
                status = rpc("mcpServerStatus/list", {"threadId": thread_id})
                assert status["data"][0]["runtimeStatus"] == "connected", status
                assert set(status["data"][0]["tools"]) == expected_tools
                result = rpc("mcpServer/tool/call", {
                    "threadId": thread_id, "server": "aiteam", "tool": "context_resolve", "arguments": {},
                })
                assert result["structuredContent"]["project"]["id"] == project_id, result
            assert _wait_for(lambda: host.live().children(recursive=True) == [], 3), [
                {"pid": child.pid, "status": child.status(), "command": child.cmdline()}
                for child in host.live().children(recursive=True)
            ]
            assert api_identity.live().children(recursive=True) == []
            print(json.dumps({"native_host_pid": host.pid, "shared_api_pid": api_identity.pid,
                              "native_threads": 2, "resident_mcp_children": 0,
                              "tool_count": len(expected_tools),
                              "cwd_isolated": True}))
