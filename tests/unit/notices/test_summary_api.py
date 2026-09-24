"""GET /api/notices/summary and the list filters the Dashboard uses (batch B)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import NoticeStatus, TaskStatus

from .test_config_change import _client


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path / 's.db'}"


@pytest.fixture(autouse=True)
async def _close():
    yield
    await close_db()


async def _register(client, key, catalog_id, host="", **params):
    body = {"key": key, "catalog_id": catalog_id, "params": params, "host": host}
    response = await client.post("/api/notices", json=body)
    assert response.status_code == 200, response.text


async def _seed(client, repo):
    await _register(client, "api_version_stale:1:2", "api_version_stale", old="v1", ver="v2")
    await _register(client, "unregistered_dir:/w", "unregistered_dir")
    await _register(client, "decisions_pending:abcd", "decisions_pending", n=1, title="t")
    await _register(client, "release_available:cc:9", "release_available", host="cc", ver="v9", old="v1")
    await _register(client, "branch_switched:x:a:b", "branch_switched", repo="r", ob="a", nb="b")
    await repo.create_briefing(title="要不要发版")
    await repo.create_briefing(title="Agent denied: Bash rm")
    await repo.create_briefing(title="auto", tags=["auto:permission-denied"])
    await repo.create_task(None, "需要拍板", tags=["requires-user-decision"])
    await repo.create_task(None, "tag prefix only", tags=["requires-user-decision-later"])
    done = await repo.create_task(None, "已完成", tags=["requires-user-decision"])
    await repo.update_task(done.id, status=TaskStatus.COMPLETED)


async def test_summary_counts_what_waits_on_the_user(db_url):
    async with _client(db_url) as (client, repo):
        await _seed(client, repo)
    await close_db()
    async with _client(db_url) as (client, _fresh):  # a new app and repository read it back
        summary = (await client.get("/api/notices/summary?language=en")).json()
        # action + decision, minus the briefing aggregate; status and immediate lines excluded
        assert summary["notices"] == 2
        assert summary["briefings"] == 1 and summary["tasks"] == 1 and summary["total"] == 4
        assert summary["top"]["key"] == "api_version_stale:1:2"  # action ranks before decision
        assert summary["top"]["user_line"].startswith("[AI Team OS] ")

        await client.post("/api/notices/api_version_stale:1:2/snooze?hours=24")
        summary = (await client.get("/api/notices/summary")).json()
        # The banner may show the briefing aggregate (newest decision); it is not counted twice.
        assert summary["notices"] == 1 and summary["top"]["key"] == "decisions_pending:abcd"


async def test_expired_snooze_counts_again(db_url):
    async with _client(db_url) as (client, repo):
        await _register(client, "api_version_stale:1:2", "api_version_stale", old="v1", ver="v2")
        await repo.set_notice_status("api_version_stale:1:2", NoticeStatus.SNOOZED, utc_now(),
                                     snoozed_until=utc_now() - timedelta(minutes=1))
        summary = (await client.get("/api/notices/summary")).json()
        assert summary["notices"] == 1 and summary["top"] is not None


async def test_list_filters_by_kind_and_group(db_url):
    async with _client(db_url) as (client, repo):
        await _seed(client, repo)
        waiting = (await client.get("/api/notices?kind=action,decision&group=queued")).json()
        assert {item["key"] for item in waiting["items"]} == {
            "api_version_stale:1:2", "unregistered_dir:/w", "decisions_pending:abcd",
        }
        recent = (await client.get("/api/notices?status=all&group=immediate")).json()
        assert [item["key"] for item in recent["items"]] == ["branch_switched:x:a:b"]
        assert (await client.get("/api/notices?group=bogus")).status_code == 422


async def test_empty_summary(db_url):
    async with _client(db_url) as (client, _repo):
        summary = (await client.get("/api/notices/summary")).json()
        assert summary == {**summary, "notices": 0, "briefings": 0, "tasks": 0, "total": 0, "top": None}


async def test_open_task_tag_count_is_a_whole_value_match(tmp_path):
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    await repo.init_db()
    await repo.create_task(None, "a", tags=["requires-user-decision", "x"])
    await repo.create_task(None, "b", tags=["x-requires-user-decision"])
    await repo.create_task(None, "c")
    assert await repo.count_open_tasks_with_tag("requires-user-decision") == 1


async def test_unknown_kind_is_rejected(db_url):
    async with _client(db_url) as (client, _repo):
        assert (await client.get("/api/notices?kind=action,bogus")).status_code == 400
        assert (await client.get("/api/notices?kind=action")).status_code == 200
