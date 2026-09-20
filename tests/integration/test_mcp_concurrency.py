"""MCP transport admission checks; all REST traffic stays in isolated apps/DBs."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest
from fastapi import FastAPI
from fastmcp import FastMCP

from aiteam.api.middleware import InputGuardrailMiddleware, SQLiteConcurrencyMiddleware

from .test_mcp_http_sharing import _call, _connect, _project
from .test_mcp_process_groups import _isolated_mcp


@pytest.mark.parametrize("json_response", [False, True], ids=["sse", "json"])
async def test_48_real_mcp_tool_calls_do_not_reacquire_transport_capacity(json_response):
    """Real MCP protocol plus nested REST; JSON mode waits to send response headers.

    The tiny REST handler intentionally has no database: this test isolates the
    admission cycle, independently of SQLite query duration or lock contention.
    """
    app = FastAPI()
    app.add_middleware(InputGuardrailMiddleware)
    app.add_middleware(SQLiteConcurrencyMiddleware, queue_timeout=5.0)
    mcp = FastMCP("ai-team-os")
    first_four = asyncio.Event()
    release = asyncio.Event()
    entered = handled = 0

    @app.post("/api/echo")
    async def echo(payload: dict) -> dict:
        nonlocal handled
        handled += 1
        await asyncio.sleep(0)  # Let the other admitted REST requests run.
        return payload

    @mcp.tool
    async def nested_rest(payload: dict) -> dict:
        nonlocal entered
        entered += 1
        if entered >= 4:
            first_four.set()
        await release.wait()
        response = await client.post("/api/echo", json=payload)
        return {"status": response.status_code, "data": response.json()}

    mcp_app = mcp.http_app(transport="streamable-http", path="/", json_response=json_response)
    app.mount("/mcp/", mcp_app)
    async with (
        mcp_app.lifespan(mcp_app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test", trust_env=False) as client,
    ):
        await _connect(client, None)
        tasks = [
            asyncio.create_task(_call(client, index + 2, "nested_rest", {"payload": {"index": index}}))
            for index in range(48)
        ]
        try:
            # In the old JSON implementation these four outer requests hold
            # every normal permit before any nested REST request can enter.
            await asyncio.wait_for(first_four.wait(), 5)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
            assert not [str(result) for result in results if isinstance(result, BaseException)]
            contents = [result["result"]["structuredContent"] for result in results]
            assert [content["status"] for content in contents] == [200] * 48
            assert [content["data"]["index"] for content in contents] == list(range(48))
            assert handled == 48

            # MCP transport exemption must not exempt the inner API input guard.
            blocked = await _call(client, 100, "nested_rest", {"payload": {"cmd": "rm -rf /"}})
            assert blocked["result"]["structuredContent"]["status"] == 400
            assert handled == 48
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert (await client.delete("/mcp/")).status_code == 200


def test_production_mcp_48_calls_and_idle_sse_streams_share_isolated_api(tmp_path):
    """Exercise production tools/context and SQLite using an owned temporary API."""
    with _isolated_mcp(tmp_path, "autostart") as runtime:
        _, _, api_identity, port, _ = runtime
        directory = tmp_path / "concurrency-project"
        project_id = _project(port, directory, "MCP concurrency")

        async def exercise():
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15,
            ) as client:
                await _connect(client, directory)
                results = await asyncio.wait_for(asyncio.gather(*(
                    _call(client, index + 2, "context_resolve") for index in range(48)
                )), 20)
                assert all(
                    result["result"]["structuredContent"]["project"]["id"] == project_id
                    for result in results
                )

                # A session supports one idle GET stream. Use distinct sessions
                # so all eight streams really stay open throughout the probe.
                async with AsyncExitStack() as streams:
                    for _ in range(8):
                        stream_client = await streams.enter_async_context(httpx.AsyncClient(
                            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5,
                        ))
                        await _connect(stream_client, directory)
                        response = await streams.enter_async_context(stream_client.stream("GET", "/mcp/"))
                        assert response.status_code == 200
                        assert response.headers["content-type"].startswith("text/event-stream")
                        assert "server-timing" not in response.headers
                    projects = await client.get("/api/projects")
                    assert projects.status_code == 200
                    resumed = await _call(client, 100, "context_resolve")
                    assert resumed["result"]["structuredContent"]["project"]["id"] == project_id
                assert (await client.delete("/mcp/")).status_code == 200

        asyncio.run(exercise())
        assert api_identity.live() is not None
