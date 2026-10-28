"""E10 channel_mention clears when its reader acks what it read (task 038b5a91).

The whole flow goes through HTTP: channel messages, the prompt that raises the
notice, the read cursor ack, then a new app with a new repository reads back.
"""

from __future__ import annotations

import pytest

from aiteam.storage.connection import close_db

from .test_notices_api import _client

CHANNEL = "team:bridge"
PROJECT = "p1"
READERS = [pytest.param("cc", "leader-cc", id="cc"), pytest.param("codex", "leader-codex", id="codex")]


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'ack.db'}"


@pytest.fixture(autouse=True)
async def _close():
    yield
    await close_db()


async def _mention(client, reader: str, text: str) -> str:
    response = await client.post(f"/api/channels/{CHANNEL}/messages", json={
        "sender": "peer", "content": text, "mentions": [reader], "project_id": PROJECT,
    })
    assert response.status_code == 201, response.text
    return response.json()["data"]["created_at"]


async def _prompt(client, host: str, reader: str) -> dict:
    response = await client.post("/api/notices/pending", json={
        "host": host, "event": "UserPromptSubmit", "source": "", "session_id": "s1",
        "reader": reader, "project_id": PROJECT,
        "facts": {"entrypoint": "cli", "fallback_language": "en"},
    })
    assert response.status_code == 200, response.text
    return response.json()


async def _ack(client, reader: str, last_read_at: str) -> None:
    response = await client.post(f"/api/channels/{CHANNEL}/read-cursor", json={
        "reader": reader, "project_id": PROJECT, "last_read_at": last_read_at,
    })
    assert response.status_code == 200, response.text


async def _read_back(db_url: str, key: str) -> dict:
    await close_db()
    async with _client(db_url) as (client, _repo):
        return (await client.get(f"/api/notices/{key}")).json()["notice"]


@pytest.mark.parametrize(("host", "reader"), READERS)
async def test_reading_every_mention_clears_the_notice(db_url, host, reader):
    async with _client(db_url) as (client, _repo):
        first = await _mention(client, reader, "请复核 M1")
        last = await _mention(client, reader, "还有 M2")
        assert "(2 new)" in (await _prompt(client, host, reader))["user_text"]
        listed = (await client.get("/api/notices?status=active")).json()["items"]
        [notice] = [item for item in listed if item["catalog_id"] == "channel_mention"]
        key = notice["key"]
        assert notice["host"] == host
        # The other host's reader acking in the same project leaves this notice alone.
        await _ack(client, "leader-codex" if reader == "leader-cc" else "leader-cc", last)
        untouched = (await client.get(f"/api/notices/{key}")).json()["notice"]
        assert untouched["status"] == "active" and untouched["params"]["n"] == 2
        # Read only the first message: one is still unread, so the notice stays with the new count.
        await _ack(client, reader, first)
    partial = await _read_back(db_url, key)
    assert partial["status"] == "active" and partial["params"]["n"] == 1
    async with _client(db_url) as (client, _repo):
        await _ack(client, reader, last)
    assert (await _read_back(db_url, key))["status"] == "cleared"
