"""Unit tests for Channel messaging API (v1.0 P1-6)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def app_client():
    """Create test client with in-memory SQLite."""
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)
    event_bus = EventBus(repo=repo)
    hook_translator = HookTranslator(repo=repo, event_bus=event_bus)

    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._hook_translator = hook_translator

    app = create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan

    client = TestClient(app)
    yield client

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


# ============================================================
# Send and Read
# ============================================================


def test_send_message_to_team_channel(app_client):
    """Send a message to a team channel and verify it is stored."""
    resp = app_client.post(
        "/api/channels/team:backend/messages",
        json={"sender": "alice", "content": "Hello backend team"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["channel"] == "team:backend"
    assert data["data"]["sender"] == "alice"
    assert data["data"]["content"] == "Hello backend team"
    assert "id" in data["data"]


def test_send_message_to_global_channel(app_client):
    """Send a message to the global channel."""
    resp = app_client.post(
        "/api/channels/global/messages",
        json={"sender": "system", "content": "Global announcement"},
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["channel"] == "global"


def test_send_message_to_project_channel(app_client):
    """Send a message to a project channel."""
    resp = app_client.post(
        "/api/channels/project:abc123/messages",
        json={"sender": "leader", "content": "Project update"},
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["channel"] == "project:abc123"


def test_read_channel_messages(app_client):
    """Read messages from a channel."""
    # Send two messages
    app_client.post(
        "/api/channels/team:frontend/messages",
        json={"sender": "bob", "content": "msg 1"},
    )
    app_client.post(
        "/api/channels/team:frontend/messages",
        json={"sender": "carol", "content": "msg 2"},
    )

    resp = app_client.get("/api/channels/team:frontend/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] == 2
    assert len(data["data"]) == 2


def test_read_channel_messages_incremental(app_client):
    """Incremental pull via 'since' parameter returns only newer messages."""
    # Send first message
    r1 = app_client.post(
        "/api/channels/team:data/messages",
        json={"sender": "agent-1", "content": "first"},
    )
    assert r1.status_code == 201
    first_ts = r1.json()["data"]["created_at"]

    # Send second message
    app_client.post(
        "/api/channels/team:data/messages",
        json={"sender": "agent-2", "content": "second"},
    )

    # Pull messages since first message timestamp
    resp = app_client.get(f"/api/channels/team:data/messages?since={first_ts}")
    assert resp.status_code == 200
    data = resp.json()
    # Should only return the second message
    assert data["total"] == 1
    assert data["data"][0]["content"] == "second"


def test_channel_isolation(app_client):
    """Messages sent to one channel do not appear in another."""
    app_client.post(
        "/api/channels/team:alpha/messages",
        json={"sender": "x", "content": "alpha msg"},
    )
    resp = app_client.get("/api/channels/team:beta/messages")
    assert resp.json()["total"] == 0


# ============================================================
# @mention
# ============================================================


def test_send_message_with_mentions(app_client):
    """Send a message with @mention tags."""
    resp = app_client.post(
        "/api/channels/global/messages",
        json={
            "sender": "leader",
            "content": "Hey @alice and @bob, please review",
            "mentions": ["@alice", "@bob"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert "@alice" in data["mentions"]
    assert "@bob" in data["mentions"]


def test_get_mentions_for_agent(app_client):
    """Get messages that @mention a specific agent."""
    # Send one message mentioning alice
    app_client.post(
        "/api/channels/global/messages",
        json={"sender": "leader", "content": "Hi @alice", "mentions": ["@alice"]},
    )
    # Send one message NOT mentioning alice
    app_client.post(
        "/api/channels/global/messages",
        json={"sender": "leader", "content": "Hi @bob", "mentions": ["@bob"]},
    )

    resp = app_client.get("/api/channels/mentions/alice")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["data"][0]["sender"] == "leader"


def test_get_mentions_no_results(app_client):
    """Returns empty list when agent has no mentions."""
    resp = app_client.get("/api/channels/mentions/nobody")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ============================================================
# Channel format validation
# ============================================================


def test_invalid_channel_format_rejected(app_client):
    """Invalid channel format returns HTTP 400."""
    resp = app_client.post(
        "/api/channels/invalid-channel/messages",
        json={"sender": "x", "content": "test"},
    )
    assert resp.status_code == 400


def test_channel_with_spaces_rejected(app_client):
    """Channel with spaces is rejected."""
    resp = app_client.post(
        "/api/channels/team:my team/messages",
        json={"sender": "x", "content": "test"},
    )
    assert resp.status_code in (400, 422)


def test_limit_parameter(app_client):
    """Limit parameter caps number of returned messages."""
    for i in range(5):
        app_client.post(
            "/api/channels/team:limited/messages",
            json={"sender": "bot", "content": f"msg {i}"},
        )

    resp = app_client.get("/api/channels/team:limited/messages?limit=3")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 3
