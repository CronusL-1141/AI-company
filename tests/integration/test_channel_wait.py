"""Real MCP -> HTTP/WebSocket -> temporary SQLite channel delivery tests."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from contextlib import suppress
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.server.middleware import Middleware
from mcp.shared.exceptions import McpError

from aiteam.api import event_bus as event_bus_module
from aiteam.api.deps import get_event_bus, get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.routes import channels, ws
from aiteam.api.ws.manager import ConnectionManager
from aiteam.mcp.tools.channels import register
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

CHANNEL = "team:wait-test"
PROJECT = "wait-project"
SINCE = "2026-01-01T00:00:00Z"
ARGS = {
    "channel": CHANNEL, "project_id": PROJECT, "reader": "receiver",
    "sender": "peer", "since": SINCE, "timeout_seconds": 0.15,
}


@pytest.fixture
async def live_channels(tmp_path, monkeypatch):
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'wait.db'}")
    await repo.init_db()
    manager = ConnectionManager()
    monkeypatch.setattr(ws, "ws_manager", manager)
    monkeypatch.setattr(event_bus_module, "ws_manager", manager)
    bus = EventBus(repo)
    app = FastAPI()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_event_bus] = lambda: bus
    app.include_router(channels.router)
    app.include_router(ws.router)
    reads = []
    request_ids = []

    class CaptureRequest(Middleware):
        async def on_call_tool(self, context, call_next):
            request_ids.append(context.fastmcp_context.request_context.request_id)
            return await call_next(context)

    @app.middleware("http")
    async def count_inbox_reads(request, call_next):
        if request.url.path.endswith("/inbox"):
            reads.append({**dict(request.query_params), "mcp_request_id": request_ids[-1] if request_ids else None})
        return await call_next(request)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    monkeypatch.setenv("AITEAM_API_URL", base)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
            mcp = FastMCP("channel-wait-test")
            mcp.add_middleware(CaptureRequest())
            register(mcp)
            async with Client(mcp) as client:
                yield client, http, repo, manager, reads
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 3)
        except TimeoutError:
            serving.cancel()
            with suppress(asyncio.CancelledError):
                await serving
        listener.close()
        await close_db()


async def send(http, **overrides):
    payload = {
        "sender": "peer", "content": "Message from peer", "mentions": ["receiver"],
        "project_id": PROJECT,
    }
    payload.update(overrides)
    response = await http.post(f"/api/channels/{CHANNEL}/messages", json=payload)
    response.raise_for_status()
    return response.json()["data"]


async def call_wait(client, **overrides):
    reply = await client.call_tool("channel_wait", {**ARGS, **overrides})
    if not reply.data["success"]:
        assert "delivery_source" not in reply.data
        assert "delivery_source" not in reply.data.get("data", {})
    return reply.data


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def delay_subscription_after_handshake(
    manager: ConnectionManager, monkeypatch: pytest.MonkeyPatch, delay: float,
) -> None:
    original_connect = manager.connect

    async def connect_before_delayed_subscription(conn_id, websocket):
        await original_connect(conn_id, websocket)
        # Delay subscription processing after the real WebSocket handshake.
        if delay:
            await asyncio.sleep(delay)

    monkeypatch.setattr(manager, "connect", connect_before_delayed_subscription)


async def test_existing_message_replays_without_ack(live_channels):
    client, http, repo, manager, _ = live_channels
    row = await send(http)
    result = await call_wait(client)
    assert result["success"] is True
    assert result["data"]["status"] == "messages"
    assert result["data"]["delivery_source"] == "replay"
    assert [m["id"] for m in result["data"]["messages"]] == [row["id"]]
    assert await repo.get_channel_cursor("receiver", CHANNEL, PROJECT) is None
    await until(lambda: manager.active_count == 0)


async def test_waiting_mcp_returns_peer_message_without_another_prompt(live_channels):
    client, http, _, manager, reads = live_channels
    waiting = asyncio.create_task(call_wait(client, timeout_seconds=2))
    await until(lambda: len(reads) == 1)
    await asyncio.sleep(0.05)
    assert not waiting.done()
    assert len(reads) == 1  # No periodic inbox reads while idle.
    row = await send(http)
    result = await asyncio.wait_for(waiting, 3)
    assert result["data"]["messages"][0]["id"] == row["id"]
    assert result["data"]["delivery_source"] == "event"
    assert len(reads) == 2
    await until(lambda: manager.active_count == 0)


async def test_stdio_mcp_process_receives_message_and_exits(live_channels):
    _, http, _, manager, reads = live_channels
    root = Path(__file__).resolve().parents[2]
    transport = StdioTransport(
        command=sys.executable,
        args=["-c", (
            "from fastmcp import FastMCP; from aiteam.mcp.tools.channels import register; "
            "mcp=FastMCP('channel-wait-stdio'); register(mcp); mcp.run(show_banner=False)"
        )],
        env={
            "PYTHONPATH": str(root / "src"), "AITEAM_API_URL": str(http.base_url).rstrip("/"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        cwd=str(root),
    )
    async with Client(transport) as client:
        assert "channel_wait" in {tool.name for tool in await client.list_tools()}
        waiting = asyncio.create_task(call_wait(client, timeout_seconds=2))
        await until(lambda: len(reads) == 1)
        row = await send(http, content="Real stdio MCP delivery")
        result = await waiting
        assert result["data"]["messages"][0]["id"] == row["id"]
        assert result["data"]["delivery_source"] == "event"
    await until(lambda: manager.active_count == 0)


async def test_timeout_preserves_cursor_and_reads_only_twice(live_channels):
    client, _, _, manager, reads = live_channels
    result = await call_wait(client)
    assert result["data"]["status"] == "timeout"
    assert result["data"]["delivery_source"] == "timeout_read"
    assert result["data"]["messages"] == []
    assert result["data"]["next_cursor"]
    assert len(reads) == 2
    await until(lambda: manager.active_count == 0)


@pytest.mark.parametrize("overrides", [
    {"project_id": "another-project"}, {"mentions": ["receiver-extra"]},
    {"mentions": ["Receiver"]}, {"sender": "receiver"}, {"sender": "another-peer"},
])
async def test_unrelated_messages_do_not_finish_wait(live_channels, overrides):
    client, http, _, _, reads = live_channels
    waiting = asyncio.create_task(call_wait(client, timeout_seconds=0.3))
    await until(lambda: len(reads) == 1)
    await send(http, **overrides)
    result = await waiting
    assert result["data"]["status"] == "timeout"
    assert result["data"]["delivery_source"] == "timeout_read"
    assert not result["data"]["messages"]


async def test_at_name_is_exact_match(live_channels):
    client, http, _, _, _ = live_channels
    row = await send(http, mentions=["@receiver"])
    assert (await call_wait(client))["data"]["messages"][0]["id"] == row["id"]


async def test_disconnect_is_error_then_replay_recovers(live_channels):
    client, http, _, manager, reads = live_channels
    waiting = asyncio.create_task(call_wait(client, timeout_seconds=2))
    await until(lambda: len(reads) == 1)
    for connection in list(manager._connections.values()):
        await connection.close(code=1012)
    result = await waiting
    assert result["success"] is False
    assert result["_error_category"] == "channel_wait_transport_error"
    assert result["resume_cursor"]
    row = await send(http)
    assert (await call_wait(client, cursor=result["resume_cursor"]))["data"]["messages"][0]["id"] == row["id"]


async def test_cancel_cleans_up_subscription(live_channels):
    client, _, _, manager, reads = live_channels
    waiting = asyncio.create_task(call_wait(client, timeout_seconds=30))
    await until(lambda: len(reads) == 1)
    await client.cancel(reads[0]["mcp_request_id"], reason="Cancel channel wait")
    with pytest.raises(McpError, match="cancelled"):
        await waiting
    await until(lambda: manager.active_count == 0)


@pytest.mark.parametrize("connection_delay", [0, 0.2])
async def test_client_timeout_does_not_implicitly_cancel_server(
    live_channels, monkeypatch, connection_delay: float,
):
    client, _, _, manager, reads = live_channels
    delay_subscription_after_handshake(manager, monkeypatch, connection_delay)
    with pytest.raises(McpError, match="Timed out"):
        await client.call_tool("channel_wait", {**ARGS, "timeout_seconds": 0.4}, timeout=0.1)
    await until(lambda: len(reads) == 1)
    assert len(reads) == 1
    assert manager.active_count == 1
    await until(lambda: manager.active_count == 0)
    assert len(reads) == 2


@pytest.mark.parametrize("connection_delay", [0, 0.2])
async def test_client_timeout_then_explicit_cancel_releases_wait(
    live_channels, monkeypatch, connection_delay: float,
):
    client, _, _, manager, reads = live_channels
    delay_subscription_after_handshake(manager, monkeypatch, connection_delay)
    with pytest.raises(McpError, match="Timed out"):
        await client.call_tool("channel_wait", {**ARGS, "timeout_seconds": 30}, timeout=0.1)
    # The client deadline does not imply that the server has reached its first read.
    await until(lambda: len(reads) == 1)
    request_id = reads[0]["mcp_request_id"]
    assert request_id is not None
    assert manager.active_count == 1
    await client.cancel(request_id, reason="Client deadline expired")
    await until(lambda: manager.active_count == 0)
    assert len(reads) == 1


async def test_subscription_timeout_has_independent_budget(live_channels, monkeypatch):
    client, http, _, manager, _ = live_channels
    original_connect = manager.connect

    async def connect_with_slow_ack(conn_id, websocket):
        await original_connect(conn_id, websocket)
        original_send = websocket.send_text

        async def send_after_delay(data):
            if json.loads(data).get("action") == "subscribe":
                await asyncio.sleep(0.2)
            await original_send(data)

        websocket.send_text = send_after_delay

    monkeypatch.setattr(manager, "connect", connect_with_slow_ack)
    row = await send(http)
    result = await call_wait(client, io_timeout_seconds=0.05)
    assert result["success"] is False
    assert result["_error_category"] == "channel_wait_transport_error"
    result = await call_wait(client, io_timeout_seconds=1)
    assert result["data"]["messages"][0]["id"] == row["id"]


async def test_lost_broadcast_is_recovered_at_deadline(live_channels, monkeypatch):
    client, http, _, manager, reads = live_channels

    async def drop(event):
        pass

    monkeypatch.setattr(manager, "broadcast_event", drop)
    waiting = asyncio.create_task(call_wait(client, timeout_seconds=0.2))
    await until(lambda: len(reads) == 1)
    row = await send(http)
    result = await waiting
    assert result["data"]["status"] == "messages"
    assert result["data"]["delivery_source"] == "timeout_read"
    assert result["data"]["messages"][0]["id"] == row["id"]


async def test_message_committed_during_first_read_is_not_lost(live_channels, monkeypatch):
    client, http, repo, _, _ = live_channels
    original = repo.list_channel_inbox
    injected = []

    async def read_then_send(*args, **kwargs):
        page = await original(*args, **kwargs)
        if not injected:
            injected.append(await send(http))
        return page

    monkeypatch.setattr(repo, "list_channel_inbox", read_then_send)
    result = await call_wait(client, timeout_seconds=1)
    assert result["data"]["messages"][0]["id"] == injected[0]["id"]


async def test_continuation_delivers_late_commit_with_old_creation_time(live_channels):
    from aiteam.storage.connection import get_session
    from aiteam.storage.models import ChannelMessageModel
    from aiteam.types import ChannelMessage

    client, http, repo, _, _ = live_channels
    delayed = ChannelMessage(
        channel=CHANNEL, project_id=PROJECT, sender="peer", mentions=["receiver"],
        content="Created first, committed last",
    )
    first = await send(http)
    page = (await call_wait(client))["data"]
    assert page["messages"][0]["id"] == first["id"]
    async with get_session(repo._db_url) as session:
        session.add(ChannelMessageModel.from_pydantic(delayed))
    next_page = (await call_wait(client, cursor=page["next_cursor"]))["data"]
    assert [row["id"] for row in next_page["messages"]] == [delayed.id]


async def test_wrong_api_response_is_error_not_empty_inbox(live_channels, monkeypatch):
    client, _, repo, _, _ = live_channels

    async def broken(**kwargs):
        raise RuntimeError("Injected database read failure")

    monkeypatch.setattr(repo, "list_channel_inbox", broken)
    result = await call_wait(client)
    assert result["success"] is False
    assert result["_error_category"] == "channel_wait_api_error"


async def test_expired_cursor_is_reported_without_advancing(live_channels):
    from sqlalchemy import delete

    from aiteam.storage.connection import get_session
    from aiteam.storage.models import ChannelMessageModel

    client, http, repo, _, _ = live_channels
    row = await send(http)
    page = (await call_wait(client))["data"]
    async with get_session(repo._db_url) as session:
        await session.execute(delete(ChannelMessageModel).where(ChannelMessageModel.id == row["id"]))
    result = await call_wait(client, cursor=page["next_cursor"])
    assert result["success"] is False
    assert result["_error_category"] == "channel_wait_cursor_expired"


@pytest.mark.parametrize("overrides", [
    {"project_id": " "}, {"reader": "receiver!"}, {"sender": "receiver"},
    {"since": "bad-time"}, {"timeout_seconds": 0}, {"timeout_seconds": 301}, {"limit": 201},
    {"io_timeout_seconds": 0}, {"io_timeout_seconds": 61},
])
async def test_invalid_input_fails_before_connecting(live_channels, overrides):
    client, _, _, manager, reads = live_channels
    result = await call_wait(client, **overrides)
    assert result["success"] is False
    assert result["_error_category"] == "invalid_input"
    assert not reads
    assert manager.active_count == 0
