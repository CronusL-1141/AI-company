"""HTTP round trips across the persistence boundary (docs/user-notice-design.md §5.5).

The whole assembly is swapped as a set: one file-backed repository serves the
routes, the ledger and every detector. A second app instance with a brand-new
repository object then reads what the first one wrote.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.services.notices.detectors import registration
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@asynccontextmanager
async def _client(db_url: str):
    repository = StorageRepository(db_url=db_url)
    await repository.init_db()
    deps._repository = repository
    deps._event_bus = EventBus(repo=repository)
    app = create_app()

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app.router.lifespan_context = no_lifespan
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, repository
    finally:
        deps._repository = None
        deps._event_bus = None


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"


@pytest.fixture(autouse=True)
async def _close():
    yield
    await close_db()


def _body(event="SessionStart", session="s1", cwd="", **facts):
    return {"host": "cc", "event": event, "source": "startup" if event == "SessionStart" else "",
            "session_id": session, "cwd": cwd,
            "facts": {"entrypoint": "cli", "fallback_language": "en", **facts}}


async def test_claim_is_visible_to_a_new_client_and_repository(db_url, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    async with _client(db_url) as (client, first):
        response = (await client.post("/api/notices/pending", json=_body(cwd=str(work)))).json()
        assert "not a registered project" in response["user_text"]
        assert len(response["delivery_ids"]) == 1
    await close_db()  # drop pooled engines: the next app must read from disk
    key = f"unregistered_dir:{registration.real_dir(str(work))}"
    async with _client(db_url) as (client, repository):
        assert repository is not first
        detail = (await client.get(f"/api/notices/{key}")).json()
        assert detail["notice"]["status"] == "active"
        assert [d["session_id"] for d in detail["deliveries"]] == ["s1"]
        assert detail["deliveries"][0]["id"] == response["delivery_ids"][0]
        assert detail["rendered"]["zh"]["user_line"].startswith("[AI Team OS] 此目录未登记")
        # Same session again in the new process: already delivered, nothing new.
        again = (await client.post("/api/notices/pending", json=_body(
            event="UserPromptSubmit", cwd=str(work), emitted=response["delivery_ids"]))).json()
        assert again["user_text"] == ""
        detail = (await client.get(f"/api/notices/{key}")).json()
        assert detail["deliveries"][0]["emitted_at"] is not None


async def test_local_record_import_is_idempotent_across_requests(db_url):
    record = {"uuid": "rec-1", "kind": "local_notice", "catalog_id": "blocked_foreign_branch",
              "key": "blocked_foreign_branch:s9:main", "params": {"branch": "main", "extra": "dropped"},
              "session_id": "s9", "event": "PreToolUse", "language": "zh", "source": "workflow_reminder"}
    for _ in range(2):
        async with _client(db_url) as (client, _):
            ok = await client.post("/api/notices/pending", json=_body(
                event="UserPromptSubmit", session="s9", local_records=[record]))
            assert ok.status_code == 200
    async with _client(db_url) as (client, _):
        detail = (await client.get("/api/notices/blocked_foreign_branch:s9:main")).json()
        assert detail["notice"]["status"] == "cleared"  # a block line is shown once, then history
        assert detail["notice"]["params"] == {"branch": "main"}
        assert len(detail["deliveries"]) == 1
        assert detail["deliveries"][0]["event"] == "local:PreToolUse"
        listed = (await client.get("/api/notices?status=all")).json()
        assert listed["total"] == 1 and listed["items"][0]["design_number"] == "E20"


async def test_consent_records_become_one_decision_event(db_url):
    record = {"uuid": "c-1", "kind": "consent", "change": "sync_installed_copies",
              "user_quote": "同步吧", "session_id": "s1"}
    async with _client(db_url) as (client, repository):
        for _ in range(3):
            await client.post("/api/notices/pending", json=_body(event="PostToolUse", local_records=[record]))
        events = await repository.list_events(event_type="decision.user_config_write")
        assert len(events) == 1 and events[0].data["user_quote"] == "同步吧"


async def test_unknown_or_non_local_records_are_ignored(db_url):
    records = [
        {"uuid": "x1", "kind": "local_notice", "catalog_id": "release_available", "key": "release_available:x"},
        {"uuid": "x2", "kind": "local_notice", "catalog_id": "nope", "key": "nope:1"},
        {"uuid": "x3", "kind": "local_notice", "catalog_id": "api_down", "key": "other:1"},
        {"kind": "local_notice", "catalog_id": "api_down", "key": "api_down"},
    ]
    async with _client(db_url) as (client, _):
        response = await client.post("/api/notices/pending", json=_body(event="PostToolUse", local_records=records))
        assert response.status_code == 200
        malformed = await client.post("/api/notices/pending", json=_body(
            event="PostToolUse", local_records=["not a record"]))
        assert malformed.status_code == 422
        listed = (await client.get("/api/notices?status=all")).json()
        assert listed["total"] == 0


async def test_api_down_import_is_history_and_install_success_clears_failures(db_url):
    records = [
        {"uuid": "a1", "kind": "local_notice", "catalog_id": "api_down", "key": "api_down", "session_id": "s1"},
        {"uuid": "f1", "kind": "local_notice", "catalog_id": "install_failed",
         "key": "install_failed:1.14.0:pep668:ab12", "variant": "pep668", "session_id": "s1"},
    ]
    async with _client(db_url) as (client, repository):
        await client.post("/api/notices/pending", json=_body(event="PostToolUse", local_records=records))
        assert (await repository.get_notice("api_down")).status.value == "cleared"
        failed = await repository.get_notice("install_failed:1.14.0:pep668:ab12")
        assert failed.status.value == "active" and failed.variant == "pep668"
        done = {"uuid": "d1", "kind": "local_notice", "catalog_id": "install_done",
                "key": "install_done:1.14.0", "params": {"ver": "v1.14.0"}, "session_id": "s2"}
        await client.post("/api/notices/pending", json=_body(event="PostToolUse", local_records=[done]))
        assert (await repository.get_notice("install_failed:1.14.0:pep668:ab12")).status.value == "cleared"


async def test_register_dismiss_snooze_and_clear(db_url):
    async with _client(db_url) as (client, _):
        bad = await client.post("/api/notices", json={"key": "x:1", "catalog_id": "x"})
        assert bad.status_code == 400
        made = await client.post("/api/notices", json={
            "key": "api_version_stale:1:2", "catalog_id": "api_version_stale",
            "params": {"old": "v1", "ver": "v2"}})
        assert made.status_code == 200 and made.json()["status"] == "active"
        snoozed = (await client.post("/api/notices/api_version_stale:1:2/snooze?hours=2")).json()
        assert snoozed["status"] == "snoozed" and snoozed["snoozed_until"]
        assert (await client.post("/api/notices/api_version_stale:1:2/snooze?hours=0")).status_code == 422
        # A snoozed notice is not offered.
        response = (await client.post("/api/notices/pending", json=_body())).json()
        assert "v1" not in response["user_text"]
        assert (await client.post("/api/notices/api_version_stale:1:2/clear")).json()["status"] == "cleared"
        assert (await client.post("/api/notices/api_version_stale:1:2/dismiss")).json()["status"] == "dismissed"
        assert (await client.post("/api/notices/missing:1/dismiss")).status_code == 404
        active = (await client.get("/api/notices")).json()
        assert active["total"] == 0


async def test_dismissing_the_folder_notice_writes_the_skip_list(db_url, tmp_path, isolated_home):
    work = tmp_path / "Work Dir"
    work.mkdir()
    async with _client(db_url) as (client, _):
        await client.post("/api/notices/pending", json=_body(cwd=str(work)))
        key = f"unregistered_dir:{registration.real_dir(str(work))}"
        assert (await client.post(f"/api/notices/{key}/dismiss")).status_code == 200
        dismissed = json.loads((isolated_home / ".claude/data/ai-team-os/dismissed_projects.json").read_text())
        assert registration.real_dir(str(work)).lower() in dismissed["dismissed"]
        second = (await client.post("/api/notices/pending", json=_body(session="s2", cwd=str(work)))).json()
        assert "registered project" not in second["user_text"]


async def test_registered_folder_gets_no_folder_notice(db_url, tmp_path):
    work = tmp_path / "proj"
    (work / "sub").mkdir(parents=True)
    async with _client(db_url) as (client, repository):
        await repository.create_project(name="p", root_path=str(work))
        response = (await client.post("/api/notices/pending", json=_body(cwd=str(work / "sub")))).json()
        assert "registered project" not in response["user_text"]


async def test_resume_and_compact_do_not_offer_registration(db_url, tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    async with _client(db_url) as (client, _):
        for source in ("resume", "compact"):
            body = _body(cwd=str(work))
            body["source"] = source
            response = (await client.post("/api/notices/pending", json=body)).json()
            assert response["user_text"] == ""


async def test_list_is_paged_and_rendered_in_the_requested_language(db_url):
    async with _client(db_url) as (client, _):
        for index in range(5):
            await client.post("/api/notices", json={
                "key": f"api_version_stale:{index}", "catalog_id": "api_version_stale",
                "params": {"old": f"v{index}", "ver": "v9"}})
        page = (await client.get("/api/notices?limit=2&offset=1&language=zh")).json()
        assert page["total"] == 5 and len(page["items"]) == 2 and page["language"] == "zh"
        item = page["items"][0]
        assert item["user_line"].startswith("[AI Team OS] 服务仍在运行")
        assert "\x1b" not in item["user_line"]
        assert item["action"] == "「重启 OS 服务」" and item["color"] == "yellow"


async def test_bad_pending_body_is_rejected(db_url):
    async with _client(db_url) as (client, _):
        assert (await client.post("/api/notices/pending", json={"host": "other", "event": "x"})).status_code == 422
        assert (await client.post("/api/notices/pending", json={"host": "cc"})).status_code == 422


async def test_local_lines_over_the_session_cap_are_registered_but_not_delivered(db_url):
    record = {"uuid": "cap-6", "kind": "local_notice", "catalog_id": "blocked_teardown",
              "key": "blocked_teardown:s1:abcd1234", "params": {"target": "wt"}, "session_id": "s1",
              "event": "PreToolUse", "displayed": False}
    async with _client(db_url) as (client, _):
        await client.post("/api/notices/pending", json=_body(event="PostToolUse", local_records=[record]))
        detail = (await client.get("/api/notices/blocked_teardown:s1:abcd1234")).json()
        assert detail["notice"]["catalog_id"] == "blocked_teardown" and detail["deliveries"] == []
