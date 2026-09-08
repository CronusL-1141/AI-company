"""Exercise the inbox API against a real temporary SQLite database."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aiteam.api.deps import get_event_bus, get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.routes.channels import router
from aiteam.storage import repository as repository_module
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

CHANNEL = "team:bridge"
PARAMS = {
    "project_id": "project-a",
    "reader": "leader-codex",
    "sender": "leader-cc",
    "since": "2026-09-08T00:00:00Z",
}


@pytest.fixture
async def inbox_client(tmp_path):
    db_file = tmp_path / "inbox.db"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db_file}")
    await repo.init_db()
    app = FastAPI()
    app.include_router(router)
    event_bus = EventBus(repo=repo)
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_event_bus] = lambda: event_bus
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            yield client, db_file
    finally:
        await close_db()


async def _send(client, db_file, *, message_id="a", created_at="2026-09-08 01:00:00.000000", **overrides):
    payload = {
        "project_id": PARAMS["project_id"],
        "sender": PARAMS["sender"],
        "mentions": [PARAMS["reader"]],
        "content": message_id,
    }
    channel = overrides.pop("channel", CHANNEL)
    payload.update(overrides)
    response = await client.post(f"/api/channels/{channel}/messages", json=payload)
    assert response.status_code == 201, response.text
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "UPDATE channel_messages SET id = ?, created_at = ? WHERE id = ?",
            (message_id, created_at, response.json()["data"]["id"]),
        )


async def _page(client, **overrides):
    response = await client.get(f"/api/channels/{CHANNEL}/inbox", params=PARAMS | overrides)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _snapshot(db_file):
    with sqlite3.connect(db_file) as conn:
        return list(conn.iterdump())


def _decode_cursor(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def _encode_cursor(payload):
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


async def test_inbox_returns_persisted_message(inbox_client):
    client, db_file = inbox_client
    await _send(client, db_file)
    page = await _page(client)
    assert [message["id"] for message in page["messages"]] == ["a"]
    assert page["has_more"] is False
    assert set(page) == {"messages", "has_more", "next_cursor"}
    assert _decode_cursor(page["next_cursor"]) == {
        "v": 1, "rowid": 1, "message_id": "a", "channel": CHANNEL,
        "project_id": PARAMS["project_id"], "reader": PARAMS["reader"], "sender": PARAMS["sender"],
    }


async def test_late_commit_with_older_timestamp_is_not_lost(inbox_client, monkeypatch):
    client, db_file = inbox_client
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db_file}")
    reached, release = asyncio.Event(), asyncio.Event()
    original = repository_module.get_session

    @asynccontextmanager
    async def delayed_session(*args, **kwargs):
        if asyncio.current_task().get_name() == "delayed-inbox-create":
            reached.set()
            await release.wait()
        async with original(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(repository_module, "get_session", delayed_session)
    delayed = asyncio.create_task(repo.create_channel_message(
        channel=CHANNEL, project_id=PARAMS["project_id"], sender=PARAMS["sender"],
        mentions=[PARAMS["reader"]], content="Created first, committed last",
    ), name="delayed-inbox-create")
    try:
        await asyncio.wait_for(reached.wait(), 3)
        await _send(client, db_file, message_id="newer",
                    created_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"))
        first = await _page(client)
        assert [row["id"] for row in first["messages"]] == ["newer"]
        release.set()
        older = await delayed
        second = await _page(client, cursor=first["next_cursor"])
        assert [row["id"] for row in second["messages"]] == [older.id]
    finally:
        release.set()
        await delayed


async def test_inbox_exact_filters_before_limit(inbox_client):
    client, db_file = inbox_client
    invalid = [
        {"project_id": "project-b"},
        {"project_id": None},
        {"channel": "team:other"},
        {"sender": "leader-cc-2"},
        {"sender": "Leader-cc"},
        {"sender": PARAMS["reader"]},
        {"mentions": []},
        {"mentions": ["leader-codex-2"]},
        {"mentions": ["Leader-codex"]},
        {"mentions": ["prefix-leader-codex"]},
    ]
    for index, override in enumerate(invalid):
        await _send(client, db_file, message_id=f"bad-{index}", **override)
    # More unrelated rows than both the page size and the legacy scan cap.
    with sqlite3.connect(db_file) as conn:
        conn.executemany(
            "INSERT INTO channel_messages "
            "(id, channel, sender, content, mentions, metadata, created_at, project_id) "
            "VALUES (?, ?, ?, '', ?, '{}', ?, ?)",
            [(f"noise-{index}", CHANNEL, PARAMS["sender"], '["leader-codex"]',
              "2026-09-08 01:00:00.000000", "project-b") for index in range(2100)],
        )
    await _send(client, db_file, message_id="wanted-1", created_at="2026-09-08 02:00:00.000000")
    await _send(client, db_file, message_id="wanted-2", mentions=["@leader-codex"],
                created_at="2026-09-08 03:00:00.000000")
    first = await _page(client, limit=1)
    assert [row["id"] for row in first["messages"]] == ["wanted-1"]
    assert first["has_more"] is True
    second = await _page(client, cursor=first["next_cursor"], limit=1)
    assert [row["id"] for row in second["messages"]] == ["wanted-2"]
    assert second["has_more"] is False
    assert (await _page(client, sender=PARAMS["reader"]))["messages"] == []


async def test_inbox_same_timestamp_pagination_and_strict_since(inbox_client):
    client, db_file = inbox_client
    for message_id in ["c", "a", "b"]:
        await _send(client, db_file, message_id=message_id)
    first = await _page(client, limit=2)
    assert [row["id"] for row in first["messages"]] == ["c", "a"]
    assert first["has_more"] is True
    second = await _page(client, limit=2, cursor=first["next_cursor"])
    assert [row["id"] for row in second["messages"]] == ["b"]
    assert second["has_more"] is False
    empty = await _page(client, cursor=second["next_cursor"])
    assert empty["messages"] == []
    assert empty["has_more"] is False
    assert empty["next_cursor"] == second["next_cursor"]
    assert (await _page(client, since="2026-09-08T01:00:00Z"))["messages"] == []


@pytest.mark.parametrize("since", ["2026-09-08T01:00:00Z", "2026-09-08T09:00:00+08:00", "2026-09-08T01:00:00"])
async def test_inbox_timezone_equivalence(inbox_client, since):
    client, db_file = inbox_client
    await _send(client, db_file, message_id="a")
    await _send(client, db_file, message_id="b", created_at="2026-09-08 01:00:00.000001")
    page = await _page(client, since=since)
    assert [row["id"] for row in page["messages"]] == ["b"]


@pytest.mark.parametrize("existing_cursor", [False, True])
async def test_inbox_is_repeatable_without_ack_or_database_writes(inbox_client, existing_cursor):
    client, db_file = inbox_client
    await _send(client, db_file)
    if existing_cursor:
        response = await client.post(
            f"/api/channels/{CHANNEL}/read-cursor",
            json={"reader": PARAMS["reader"], "project_id": PARAMS["project_id"],
                  "last_read_at": "2026-09-08T02:00:00Z"},
        )
        assert response.status_code == 200, response.text
    before = _snapshot(db_file)
    first = await _page(client)
    assert [row["id"] for row in first["messages"]] == ["a"]
    assert await _page(client) == first
    assert _snapshot(db_file) == before
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT count(*) FROM channel_read_cursors").fetchone()[0] == int(existing_cursor)


@pytest.mark.parametrize("missing", ["project_id", "reader", "sender"])
async def test_inbox_required_parameters(inbox_client, missing):
    client, _ = inbox_client
    params = PARAMS.copy()
    del params[missing]
    response = await client.get(f"/api/channels/{CHANNEL}/inbox", params=params)
    assert response.status_code == 422


@pytest.mark.parametrize(("overrides", "status"), [
    ({"project_id": ""}, 400),
    ({"project_id": "   "}, 400),
    ({"reader": ""}, 400),
    ({"reader": "@leader-codex"}, 400),
    ({"sender": ""}, 400),
    ({"sender": "@leader-cc"}, 400),
    ({"sender": "x" * 101}, 400),
    ({"since": "not-a-time"}, 422),
    ({"limit": 0}, 422),
    ({"limit": 201}, 422),
    ({"limit": "invalid"}, 422),
    ({"cursor": "x" * 4097}, 422),
])
async def test_inbox_invalid_parameters(inbox_client, overrides, status):
    client, _ = inbox_client
    response = await client.get(f"/api/channels/{CHANNEL}/inbox", params=PARAMS | overrides)
    assert response.status_code == status


async def test_inbox_invalid_channel(inbox_client):
    client, _ = inbox_client
    response = await client.get("/api/channels/not-valid/inbox", params=PARAMS)
    assert response.status_code == 400


async def test_inbox_requires_since_or_cursor(inbox_client):
    client, _ = inbox_client
    response = await client.get(f"/api/channels/{CHANNEL}/inbox", params={
        key: value for key, value in PARAMS.items() if key != "since"
    })
    assert response.status_code == 400


@pytest.mark.parametrize("unrelated_row", [False, True])
async def test_empty_scope_cursor_captures_future_commit_with_old_time(inbox_client, unrelated_row):
    client, db_file = inbox_client
    if unrelated_row:
        await _send(client, db_file, project_id="other-project")
    empty = await _page(client)
    assert empty["messages"] == []
    assert empty["next_cursor"]
    assert _decode_cursor(empty["next_cursor"])["rowid"] == 0
    assert _decode_cursor(empty["next_cursor"])["message_id"] == ""
    await _send(client, db_file, message_id="late", created_at="2025-01-01 00:00:00.000000")
    params = {key: value for key, value in PARAMS.items() if key != "since"}
    response = await client.get(f"/api/channels/{CHANNEL}/inbox", params=params | {"cursor": empty["next_cursor"]})
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["data"]["messages"]] == ["late"]


async def test_initial_empty_page_anchors_actual_scan_upper_bound(inbox_client):
    client, db_file = inbox_client
    await _send(client, db_file, message_id="old", created_at="2025-01-01 00:00:00.000000")
    await _send(client, db_file, message_id="other-recipient", mentions=["somebody-else"])
    await _send(client, db_file, message_id="other-project", project_id="other-project")
    empty = await _page(client)
    assert empty["messages"] == []
    anchor = _decode_cursor(empty["next_cursor"])
    assert (anchor["rowid"], anchor["message_id"]) == (2, "other-recipient")
    await _send(client, db_file, message_id="late", created_at="2025-01-01 00:00:00.000000")
    page = await _page(client, cursor=empty["next_cursor"], since="2099-01-01T00:00:00Z")
    assert [row["id"] for row in page["messages"]] == ["late"]


@pytest.mark.parametrize("replace_anchor", [False, True])
async def test_deleted_or_reused_rowid_expires_cursor(inbox_client, replace_anchor):
    client, db_file = inbox_client
    await _send(client, db_file)
    page = await _page(client)
    with sqlite3.connect(db_file) as conn:
        conn.execute("DELETE FROM channel_messages WHERE id = 'a'")
    if replace_anchor:
        await _send(client, db_file, message_id="replacement")
        with sqlite3.connect(db_file) as conn:
            assert conn.execute("SELECT rowid FROM channel_messages WHERE id = 'replacement'").fetchone()[0] == 1
    response = await client.get(f"/api/channels/{CHANNEL}/inbox", params=PARAMS | {"cursor": page["next_cursor"]})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "cursor_expired"


@pytest.mark.parametrize("column", ["project_id", "channel"])
async def test_moved_anchor_expires_cursor(inbox_client, column):
    client, db_file = inbox_client
    await _send(client, db_file)
    page = await _page(client)
    with sqlite3.connect(db_file) as conn:
        conn.execute(f"UPDATE channel_messages SET {column} = ? WHERE id = ?", ("other", "a"))
    response = await client.get(f"/api/channels/{CHANNEL}/inbox", params=PARAMS | {"cursor": page["next_cursor"]})
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("field", ["project_id", "channel", "reader", "sender"])
async def test_cursor_cannot_cross_scope(inbox_client, field):
    client, db_file = inbox_client
    await _send(client, db_file)
    page = await _page(client)
    channel = "team:other" if field == "channel" else CHANNEL
    params = PARAMS | {"cursor": page["next_cursor"]}
    if field != "channel":
        params[field] = "other"
    response = await client.get(f"/api/channels/{channel}/inbox", params=params)
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("cursor", ["!invalid!", "a", "e30", "bnVsbA", "W10", "_w"])
async def test_malformed_cursor_returns_400(inbox_client, cursor):
    client, _ = inbox_client
    response = await client.get(f"/api/channels/{CHANNEL}/inbox", params=PARAMS | {"cursor": cursor})
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("changes", [
    {"v": 2}, {"v": True}, {"rowid": -1}, {"rowid": True}, {"rowid": 1.5},
    {"rowid": 2**63}, {"rowid": 0}, {"message_id": ""}, {"message_id": 1}, {"extra": "field"},
])
async def test_cursor_payload_validation(inbox_client, changes):
    client, db_file = inbox_client
    await _send(client, db_file)
    page = await _page(client)
    cursor = _encode_cursor(_decode_cursor(page["next_cursor"]) | changes)
    response = await client.get(f"/api/channels/{CHANNEL}/inbox", params=PARAMS | {"cursor": cursor})
    assert response.status_code == 400, response.text
