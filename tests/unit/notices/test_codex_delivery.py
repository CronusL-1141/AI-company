"""Conservative Codex surface: one visible claim, safe retry, persistent evidence."""
from datetime import timedelta

import pytest

from aiteam.clock import utc_now
from aiteam.services.notices import ledger
from aiteam.services.notices.catalog import CATALOG, render_entry
from aiteam.services.notices.render import display_width
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

from .conftest import StubDetector, finding, request


def candidates():
    return StubDetector("codex-items", ("codex_copy_stale", "api_version_stale"), [
        finding("codex_copy_stale", "install-a", n=2),
        finding("api_version_stale", "v1:v2", old="v1", ver="v2"),
    ])


async def test_one_line_claims_only_one_and_carries_the_other_across_db_reopen(repo):
    now = utc_now()
    first = await ledger.pending(repo, request(host="codex"), registry=[candidates()], now=now)
    assert len(first.delivery_ids) == 1
    assert len(first.user_text.splitlines()) == 1
    assert "; 1 more pending" in first.user_text
    assert display_width(first.user_text) <= 160
    assert "\x1b" not in first.user_text
    assert len(await repo.list_notice_deliveries(host="codex", session_id="s1")) == 1
    url = repo._db_url
    await close_db()
    reopened = StorageRepository(db_url=url)
    await reopened.init_db()
    second = await ledger.pending(reopened, request(host="codex", event="UserPromptSubmit",
                                  emitted=first.delivery_ids), registry=[candidates()], now=now+timedelta(seconds=1))
    assert len(second.delivery_ids) == 1
    assert not set(first.delivery_ids) & set(second.delivery_ids)
    rows = await reopened.list_notice_deliveries(host="codex", session_id="s1")
    assert len(rows) == 2
    assert next(row for row in rows if row.id == first.delivery_ids[0]).emitted_at is not None


async def test_resume_does_not_confirm_from_model_text_and_retries_action_only_once(repo, tmp_path):
    now = utc_now()
    stub = StubDetector("action", ("codex_copy_stale",), [finding("codex_copy_stale", "x", n=1)])
    first = await ledger.pending(repo, request(host="codex", source="resume"), registry=[stub], now=now)
    row = (await repo.list_notice_deliveries(host="codex", session_id="s1"))[0]
    assert row.channel_reliable is False and row.confirmed_at is None
    assert "tried to show" in first.model_text
    prompt = await ledger.pending(repo, request(host="codex", event="UserPromptSubmit",
                                  emitted=first.delivery_ids), registry=[stub], now=now+timedelta(seconds=1))
    assert prompt.delivery_ids == first.delivery_ids
    await close_db()
    reopened = StorageRepository(db_url=repo._db_url)
    await reopened.init_db()
    again = await ledger.pending(reopened, request(host="codex", event="UserPromptSubmit",
                                 emitted=prompt.delivery_ids), registry=[stub], now=now+timedelta(seconds=2))
    assert again.user_text == ""
    row = (await reopened.list_notice_deliveries(host="codex", session_id="s1"))[0]
    assert row.refired_at is not None


async def test_resume_unknown_status_is_not_refired_or_confirmed(repo):
    now = utc_now()
    stub = StubDetector("release", ("release_available",), [
        finding("release_available", "codex:v2", ver="v2", old="v1", url="")])
    first = await ledger.pending(repo, request(host="codex", source="resume"), registry=[stub], now=now)
    # Release catalog can limit sources; use a registered prompt status instead.
    await ledger.register(repo, catalog_id="channel_mention", key="channel_mention:codex:p:t",
                          host="codex", params={"n":1,"sender":"s","channel":"c","details":"d"})
    from aiteam.types import NoticeDelivery
    delivery = NoticeDelivery(key="channel_mention:codex:p:t", host="codex", session_id="s1",
                              event="SessionStart:resume", channel_reliable=False, language="en", claimed_at=now)
    assert await repo.claim_notice_delivery(delivery, inflight_after=now-timedelta(seconds=60))
    prompt = await ledger.pending(repo, request(host="codex", event="UserPromptSubmit",
                                  emitted=[delivery.id, *first.delivery_ids]),
                                  registry=[], now=now+timedelta(seconds=1))
    assert delivery.id not in prompt.delivery_ids
    row = next(r for r in await repo.list_notice_deliveries(host="codex", session_id="s1") if r.id == delivery.id)
    assert row.confirmed_at is None and row.refired_at is None


@pytest.mark.parametrize("language", ["zh", "en"])
def test_inline_count_fits_without_truncating_upgrade_command(language):
    result = render_entry(CATALOG["release_available"], variant="codex", language=language, host="codex",
                          params={"ver":"v12345678901", "old":"v12345678901", "url":""}, pending_count=9999)
    assert "python3 scripts/codex_adapter.py upgrade" in result.line
    assert display_width(result.line) <= 160
    assert "9999" in result.line


async def test_concurrent_first_turn_never_claims_a_notice_twice(repo):
    import asyncio
    stub = StubDetector("action", ("codex_copy_stale",), [finding("codex_copy_stale", "x", n=1)])
    outputs = await asyncio.gather(*[
        ledger.pending(repo, request(host="codex", event=event), registry=[stub])
        for event in ("SessionStart", "UserPromptSubmit")
    ])
    assert sum(len(r.delivery_ids) for r in outputs) == 1
    assert len(await repo.list_notice_deliveries(host="codex", session_id="s1")) == 1


async def test_pending_returns_persisted_project_binding_after_reopen(tmp_path):
    from .test_notices_api import _client

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'binding.db'}"
    work = tmp_path / "work"
    work.mkdir()
    async with _client(db_url) as (client, _):
        created = await client.post("/api/projects", json={"name": "binding", "root_path": str(work)})
        assert created.status_code in (200, 201), created.text
        project_id = created.json()["data"]["id"]
    await close_db()
    async with _client(db_url) as (client, _):
        response = await client.post("/api/notices/pending", json={
            "host": "codex", "event": "UserPromptSubmit", "session_id": "binding-session",
            "cwd": str(work), "reader": "leader-codex",
        })
        assert response.status_code == 200, response.text
        assert response.json()["project_id"] == project_id
