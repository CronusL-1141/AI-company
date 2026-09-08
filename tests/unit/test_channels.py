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


# ════════════════════════════════════════════════════════════════
# 未读徽章（2026-09-08）
#
# 这一组守的是几个「全绿但用户那边什么都不发生」的失败形态：路由被通配吃掉、
# 取摘要顺手消掉未读、水位按 now 推进跳过没读到的消息。断言一律跨请求，不看
# 内存对象——单进程里拼出来的数"对"不算数。
# ════════════════════════════════════════════════════════════════

_PROJ = "proj-unread-test"
_CHAN = "team:bridge"


def _send(client, *, sender, mentions, project_id=_PROJ, channel=_CHAN, content="正文"):
    return client.post(
        f"/api/channels/{channel}/messages",
        json={
            "sender": sender,
            "content": content,
            "mentions": mentions,
            "project_id": project_id,
        },
    )


def _unread(client, reader="leader-cc", project_id=_PROJ):
    return client.get(
        "/api/channels/unread", params={"reader": reader, "project_id": project_id}
    )


def test_unread_route_is_not_shadowed_by_channel_wildcard(app_client):
    """/unread 不能被 /{channel}/messages 的通配吃掉。

    一旦被吃掉，_validate_channel 会把它判成非法频道回 400，而 hook 侧对失败是静默
    降级的——最终表现是"徽章从来不亮"，日志里没有任何东西指向真正的原因。
    """
    resp = _unread(app_client)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total"] == 0


def test_unread_counts_then_clears_across_requests(app_client):
    """完整闭环：发 → 有未读 → 推进到实际读到那条 → 归零 → 再查仍是零。"""
    sent = _send(app_client, sender="leader-codex", mentions=["leader-cc"])
    assert sent.status_code == 201, sent.text
    created_at = sent.json()["data"]["created_at"]

    before = _unread(app_client).json()["data"]
    assert before["total"] == 1
    assert before["channels"][0]["channel"] == _CHAN
    assert before["channels"][0]["latest_sender"] == "leader-codex"

    adv = app_client.post(
        f"/api/channels/{_CHAN}/read-cursor",
        json={"reader": "leader-cc", "project_id": _PROJ, "last_read_at": created_at},
    )
    assert adv.status_code == 200, adv.text
    assert adv.json()["data"]["advanced"] is True

    assert _unread(app_client).json()["data"]["total"] == 0
    assert _unread(app_client).json()["data"]["total"] == 0


def test_reading_unread_does_not_clear_it(app_client):
    """取摘要绝不消未读——否则 hook 一查，用户还没看见就已经"已读"了。"""
    _send(app_client, sender="leader-codex", mentions=["leader-cc"])
    assert _unread(app_client).json()["data"]["total"] == 1
    assert _unread(app_client).json()["data"]["total"] == 1
    assert _unread(app_client).json()["data"]["total"] == 1


def test_cursor_advance_is_monotonic_across_requests(app_client):
    """水位不回退：拿旧时间戳再推一次应当 advanced=false 且未读不复活。"""
    sent = _send(app_client, sender="leader-codex", mentions=["leader-cc"])
    created_at = sent.json()["data"]["created_at"]

    app_client.post(
        f"/api/channels/{_CHAN}/read-cursor",
        json={"reader": "leader-cc", "project_id": _PROJ, "last_read_at": created_at},
    )
    again = app_client.post(
        f"/api/channels/{_CHAN}/read-cursor",
        json={
            "reader": "leader-cc",
            "project_id": _PROJ,
            "last_read_at": "2000-01-01T00:00:00+00:00",
        },
    )
    assert again.json()["data"]["advanced"] is False
    assert _unread(app_client).json()["data"]["total"] == 0


def test_unread_is_scoped_to_project(app_client):
    """指定项目的收件人才看得到，避免读错项目的信。"""
    _send(app_client, sender="leader-codex", mentions=["leader-cc"], project_id="other-proj")
    assert _unread(app_client).json()["data"]["total"] == 0
    assert _unread(app_client, project_id="other-proj").json()["data"]["total"] == 1


def test_unread_requires_reader_and_project(app_client):
    assert app_client.get("/api/channels/unread", params={"project_id": _PROJ}).status_code == 422
    assert app_client.get("/api/channels/unread", params={"reader": "leader-cc"}).status_code == 422
    bad = app_client.get(
        "/api/channels/unread", params={"reader": "not a reader!", "project_id": _PROJ}
    )
    assert bad.status_code == 400


def test_message_without_project_stays_sendable_but_uncounted(app_client):
    """发送侧宽容：不带 project_id 照发照存，只是不进任何项目的未读。

    这条守的是一个刻意的取舍——曾经写成"推断不出项目就 400"，那会把约束加在整个
    channel API 上，连从不用未读功能的历史调用方一起打死。
    """
    resp = app_client.post(
        f"/api/channels/{_CHAN}/messages",
        json={"sender": "legacy", "content": "老调用方", "mentions": ["leader-cc"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["project_id"] is None
    assert _unread(app_client).json()["data"]["total"] == 0


def test_project_channel_infers_its_own_project(app_client):
    """project:<id> 频道不必手填 project_id。"""
    resp = app_client.post(
        "/api/channels/project:abc123/messages",
        json={"sender": "leader-codex", "content": "x", "mentions": ["leader-cc"]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["project_id"] == "abc123"
    assert _unread(app_client, project_id="abc123").json()["data"]["total"] == 1
