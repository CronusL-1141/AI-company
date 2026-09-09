"""Slow WebSocket clients must not hold hook acknowledgements indefinitely."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from time import perf_counter

import httpx
import pytest
from fastapi import FastAPI

from aiteam.api import event_bus as bus_module
from aiteam.api.deps import get_hook_translator
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes.hooks import router
from aiteam.api.ws.manager import ConnectionManager
from aiteam.api.ws.protocol import WSEvent
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository


class Peer:
    def __init__(self, *, blocked: bool = False) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.messages: list[str] = []
        self.closed: list[int] = []
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_release.set()
        self.fail_send = False
        if not blocked:
            self.release.set()

    async def accept(self) -> None:
        pass

    async def send_text(self, message: str) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.fail_send:
            raise RuntimeError("transport failed")
        self.messages.append(message)

    async def close(self, code: int) -> None:
        self.close_started.set()
        await self.close_release.wait()
        self.closed.append(code)


@pytest.mark.asyncio
async def test_hook_commits_and_acknowledges_with_blocked_websocket(tmp_path, monkeypatch):
    db_path = tmp_path / "hook.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    repo = StorageRepository(db_url=db_url)
    await repo.init_db()
    manager = ConnectionManager()
    slow = Peer(blocked=True)
    fast = Peer()
    await manager.connect("slow", slow)
    await manager.connect("fast", fast)
    manager.subscribe("slow", "cc.*")
    manager.subscribe("fast", "cc.*")
    monkeypatch.setattr(bus_module, "ws_manager", manager)
    monkeypatch.setattr(bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.delenv("AITEAM_HOOK_RAW_DUMP", raising=False)
    translator = HookTranslator(repo=repo, event_bus=EventBus(repo))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_hook_translator] = lambda: translator
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "ack-latency-probe",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/synthetic-probe"},
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            started = perf_counter()
            request = asyncio.create_task(client.post("/api/hooks/event", json=payload))
            try:
                await asyncio.wait_for(slow.started.wait(), timeout=2)
                with sqlite3.connect(db_path) as reader:
                    count = reader.execute(
                        "SELECT COUNT(*) FROM events WHERE type = ? AND source = ?",
                        ("cc.tool_use", "session:ack-latency-probe"),
                    ).fetchone()[0]
                assert count == 1
                print("independent SQLite read: cc.tool_use committed while WS blocked")
                response = await asyncio.wait_for(request, timeout=1)
                elapsed = perf_counter() - started
                print(f"hook HTTP acknowledgement: {elapsed:.3f}s")
                assert response.status_code == 200
                assert response.json() == {"decision": "allow"}
                assert elapsed < 1
                assert slow.cancelled.is_set()
                assert slow.closed == [1013]
                assert manager.active_count == 1
                assert len(fast.messages) == 1
                assert json.loads(fast.messages[0])["channel"] == "cc.tool_use"
                assert len(await repo.list_events(event_type="cc.tool_use")) == 1
            finally:
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    finally:
        await get_engine(db_url).dispose()


def make_event(channel: str = "cc.tool_use") -> WSEvent:
    return WSEvent(channel=channel, event_type=channel)


@pytest.mark.asyncio
async def test_many_slow_peers_do_not_delay_fast_peer_or_multiply_budget():
    manager = ConnectionManager(send_timeout=0.05)
    slow_peers = [Peer(blocked=True) for _ in range(10)]
    for index, peer in enumerate(slow_peers):
        await manager.connect(str(index), peer)
        manager.subscribe(str(index), "*")
    fast = Peer()
    await manager.connect("fast", fast)
    manager.subscribe("fast", "cc.*")
    manager.subscribe("fast", "*")
    started = perf_counter()
    broadcast = asyncio.create_task(manager.broadcast_event(make_event()))
    try:
        await asyncio.wait_for(fast.started.wait(), timeout=0.03)
        assert not broadcast.done()
        await asyncio.wait_for(broadcast, timeout=0.3)
        assert perf_counter() - started < 0.3
        assert len(fast.messages) == 1
        assert manager.active_count == 1
        assert all(peer.cancelled.is_set() for peer in slow_peers)
        assert all(peer.closed == [1013] for peer in slow_peers)
        assert manager._channel_index == {"cc.*": {"fast"}, "*": {"fast"}}
    finally:
        if not broadcast.done():
            broadcast.cancel()
        await asyncio.gather(broadcast, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "transport"])
async def test_old_send_failure_preserves_replacement_identity(failure):
    manager = ConnectionManager(send_timeout=0.05)
    old = Peer(blocked=True)
    await manager.connect("reused", old)
    manager.subscribe("reused", "old.*")
    broadcast = asyncio.create_task(manager.broadcast_event(make_event("old.event")))
    try:
        await asyncio.wait_for(old.started.wait(), timeout=1)
        replacement = Peer()
        await manager.connect("reused", replacement)
        manager.subscribe("reused", "new.*")
        if failure == "transport":
            old.fail_send = True
            old.release.set()
        await asyncio.wait_for(broadcast, timeout=1)
        assert manager._connections["reused"] is replacement
        assert manager._channel_index == {"new.*": {"reused"}}
        assert replacement.closed == []
        assert old.closed == [1013]
        await manager.broadcast_event(make_event("new.event"))
        assert len(replacement.messages) == 1
    finally:
        if not broadcast.done():
            broadcast.cancel()
        await asyncio.gather(broadcast, return_exceptions=True)


@pytest.mark.asyncio
async def test_stalled_close_has_a_bound_and_does_not_close_new_peer():
    manager = ConnectionManager(send_timeout=0.03)
    old = Peer(blocked=True)
    old.close_release.clear()
    await manager.connect("reused", old)
    manager.subscribe("reused", "*")
    started = perf_counter()
    broadcast = asyncio.create_task(manager.broadcast_event(make_event()))
    try:
        await asyncio.wait_for(old.close_started.wait(), timeout=1)
        assert manager.active_count == 0
        replacement = Peer()
        await manager.connect("reused", replacement)
        manager.subscribe("reused", "*")
        await asyncio.wait_for(broadcast, timeout=0.3)
        assert perf_counter() - started < 0.3
        assert manager._connections["reused"] is replacement
        assert replacement.closed == []
    finally:
        if not broadcast.done():
            broadcast.cancel()
        await asyncio.gather(broadcast, return_exceptions=True)


@pytest.mark.asyncio
async def test_legacy_close_cancellation_does_not_hold_broadcast():
    from websockets.legacy.server import serve

    ready = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def handler(websocket):
        ready.set_result(websocket)
        await release.wait()

    async with serve(handler, "127.0.0.1", 0, close_timeout=0.6) as server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write((
                f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1)
            websocket = await asyncio.wait_for(ready, timeout=1)

            class LegacyPeer:
                async def accept(self):
                    pass

                async def send_text(self, message):
                    await websocket.send(message)

                async def close(self, code):
                    await websocket.close(code)

            manager = ConnectionManager(send_timeout=0.05)
            await manager.connect("legacy", LegacyPeer())
            manager.subscribe("legacy", "*")
            # Drain resumes after send timeout, before close timeout. The raw
            # client keeps TCP open without answering the closing handshake.
            websocket.pause_writing()
            asyncio.get_running_loop().call_later(0.07, websocket.resume_writing)
            started = perf_counter()
            await manager.broadcast_event(make_event())
            elapsed = perf_counter() - started
            print(f"legacy closing handshake: broadcast returned in {elapsed:.3f}s")
            assert elapsed < 0.3
            assert manager.active_count == 0
            assert len(manager._cleanup_tasks) == 1
        finally:
            writer.close()
            await writer.wait_closed()
            release.set()
    await asyncio.sleep(0)
    assert not manager._cleanup_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_broadcast", [False, True])
async def test_cancel_resistant_cleanup_is_tracked_until_failure(caplog, cancel_broadcast):
    class SlowClosePeer(Peer):
        async def close(self, code):
            self.close_started.set()
            try:
                await self.close_release.wait()
            except asyncio.CancelledError:
                await self.close_release.wait()
            raise RuntimeError("late close failure")

    caplog.set_level("DEBUG", logger="aiteam.api.ws.manager")
    manager = ConnectionManager(send_timeout=0.03)
    peer = SlowClosePeer()
    peer.fail_send = True
    peer.close_release.clear()
    await manager.connect("peer", peer)
    manager.subscribe("peer", "*")
    broadcast = asyncio.create_task(manager.broadcast_event(make_event()))
    try:
        await asyncio.wait_for(peer.close_started.wait(), timeout=1)
        if cancel_broadcast:
            broadcast.cancel()
            with pytest.raises(asyncio.CancelledError):
                await broadcast
        else:
            await asyncio.wait_for(broadcast, timeout=0.3)
        assert manager.active_count == 0
        assert len(manager._cleanup_tasks) == 1
        assert not next(iter(manager._cleanup_tasks)).done()
    finally:
        peer.close_release.set()
        await asyncio.gather(broadcast, *manager._cleanup_tasks, return_exceptions=True)
        await asyncio.sleep(0)
    assert not manager._cleanup_tasks
    assert "WS close failed" in caplog.text
    assert "late close failure" in caplog.text


@pytest.mark.asyncio
async def test_cancellation_propagates_without_orphaned_sends():
    manager = ConnectionManager(send_timeout=10)
    peers = [Peer(blocked=True), Peer(blocked=True)]
    for index, peer in enumerate(peers):
        await manager.connect(str(index), peer)
        manager.subscribe(str(index), "*")
    broadcast = asyncio.create_task(manager.broadcast_event(make_event()))
    try:
        await asyncio.wait_for(
            asyncio.gather(*(peer.started.wait() for peer in peers)), timeout=1
        )
    finally:
        broadcast.cancel()
        with pytest.raises(asyncio.CancelledError):
            await broadcast
    assert all(peer.cancelled.is_set() for peer in peers)
    assert all(peer.closed == [] for peer in peers)


@pytest.mark.asyncio
async def test_channel_matching_and_unsubscribe_keep_non_targets_untouched():
    manager = ConnectionManager()
    peer = Peer()
    await manager.connect("peer", peer)
    manager.subscribe("peer", "team.*")
    await manager.broadcast_event(make_event())
    assert peer.messages == []
    manager.subscribe("peer", "cc.*")
    manager.unsubscribe("peer", "cc.*")
    await manager.broadcast_event(make_event())
    assert peer.messages == []
    manager.disconnect("peer")
    assert manager._channel_index == {}


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_push_budget_must_be_positive_and_finite(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        ConnectionManager(send_timeout=timeout)
