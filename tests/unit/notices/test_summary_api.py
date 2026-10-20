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


@pytest.fixture()
def folder_key(tmp_path):
    """An unregistered folder that exists: the on-demand sweep clears vanished ones."""
    folder = tmp_path / "w"
    folder.mkdir()
    return f"unregistered_dir:{folder}"


async def _register(client, key, catalog_id, host="", **params):
    body = {"key": key, "catalog_id": catalog_id, "params": params, "host": host}
    response = await client.post("/api/notices", json=body)
    assert response.status_code == 200, response.text


async def _seed(client, repo, folder_key):
    await _register(client, "api_version_stale:1:2", "api_version_stale", old="v1", ver="v2")
    await _register(client, folder_key, "unregistered_dir")
    await _register(client, "decisions_pending:abcd", "decisions_pending", n=1, title="t")
    await _register(client, "release_available:cc:9", "release_available", host="cc", ver="v9", old="v1")
    await _register(client, "branch_switched:x:a:b", "branch_switched", repo="r", ob="a", nb="b")
    await repo.create_briefing(title="要不要发版")
    await repo.create_briefing(title="Agent denied: Bash rm")
    await repo.create_briefing(title="auto", tags=["auto:permission-denied"])
    await repo.create_task(None, "需要拍板",
                           config={"task_management": {"waiting_on": {"kind": "user", "reason": "请决定"}}})
    await repo.create_task(None, "tag prefix only", tags=["requires-user-decision-later"])
    done = await repo.create_task(None, "已完成",
                                  config={"task_management": {"waiting_on": {"kind": "user", "reason": "已决定"}}})
    await repo.update_task(done.id, status=TaskStatus.COMPLETED)


async def test_summary_counts_what_waits_on_the_user(db_url, folder_key):
    async with _client(db_url) as (client, repo):
        await _seed(client, repo, folder_key)
    await close_db()
    async with _client(db_url) as (client, _fresh):  # a new app and repository read it back
        summary = (await client.get("/api/notices/summary?language=en")).json()
        # action + decision, minus the briefing aggregate; status and immediate lines excluded
        assert summary["notices"] == 2
        assert summary["briefings"] == 1 and summary["tasks"] == 1 and summary["total"] == 4
        assert summary["top"]["key"] == "api_version_stale:1:2"  # action ranks before decision
        assert summary["top_count"] == 1 and summary["others"] == 3
        assert summary["top"]["user_line"].startswith("[AI Team OS] ")

        await client.post("/api/notices/api_version_stale:1:2/snooze?hours=24")
        summary = (await client.get("/api/notices/summary")).json()
        # The banner may show the briefing aggregate (newest decision); it is not counted twice.
        assert summary["notices"] == 1 and summary["top"]["key"] == "decisions_pending:abcd"
        assert summary["top_count"] == 1 and summary["others"] == 2


async def test_expired_snooze_counts_again(db_url):
    async with _client(db_url) as (client, repo):
        await _register(client, "api_version_stale:1:2", "api_version_stale", old="v1", ver="v2")
        await repo.set_notice_status("api_version_stale:1:2", NoticeStatus.SNOOZED, utc_now(),
                                     snoozed_until=utc_now() - timedelta(minutes=1))
        summary = (await client.get("/api/notices/summary")).json()
        assert summary["notices"] == 1 and summary["top"] is not None


async def test_list_filters_by_kind_and_group(db_url, folder_key):
    async with _client(db_url) as (client, repo):
        await _seed(client, repo, folder_key)
        waiting = (await client.get("/api/notices?kind=action,decision&group=queued")).json()
        assert {item["key"] for item in waiting["items"]} == {
            "api_version_stale:1:2", folder_key, "decisions_pending:abcd",
        }
        recent = (await client.get("/api/notices?status=all&group=immediate")).json()
        assert [item["key"] for item in recent["items"]] == ["branch_switched:x:a:b"]
        assert (await client.get("/api/notices?group=bogus")).status_code == 422


async def test_empty_summary(db_url):
    async with _client(db_url) as (client, _repo):
        summary = (await client.get("/api/notices/summary")).json()
        assert summary == {**summary, "notices": 0, "briefings": 0, "tasks": 0, "total": 0,
                           "top": None, "top_count": 0, "others": 0}


@pytest.mark.parametrize("language", ["zh", "en"])
async def test_aggregate_banner_excludes_every_represented_briefing(db_url, folder_key, language):
    async with _client(db_url) as (client, repo):
        for title in ("决定一", "决定二", "决定三"):
            await repo.create_briefing(title=title)
        await repo.create_briefing(title="Agent denied: automatic")
        await repo.create_briefing(title="automatic", tags=["auto:permission-denied"])
        # The detector's old n must not be parsed or trusted for current counts.
        await _register(client, "decisions_pending:test", "decisions_pending", n=99, title="old snapshot")
    await close_db()
    async with _client(db_url) as (client, _repo):
        summary = (await client.get(f"/api/notices/summary?language={language}")).json()
        assert summary["top"]["catalog_id"] == "decisions_pending"
        assert summary["total"] == summary["briefings"] == summary["top_count"] == 3
        assert summary["others"] == 0
        assert "3" in summary["top"]["user_line"] and "99" not in summary["top"]["user_line"]
        assert "决定三" in summary["top"]["user_line"]
        stored = (await client.get("/api/notices/decisions_pending:test")).json()
        assert stored["notice"]["params"] == {"n": 99, "title": "old snapshot"}
        await _register(client, folder_key, "unregistered_dir")
        await _register(client, "decisions_pending:test", "decisions_pending", n=3, title="决定三")
    await close_db()
    async with _client(db_url) as (client, _repo):
        summary = (await client.get(f"/api/notices/summary?language={language}")).json()
        assert summary["total"] == 4 and summary["notices"] == 1
        assert summary["top_count"] == 3 and summary["others"] == 1
        assert summary["top"]["catalog_id"] == "decisions_pending"


async def test_aggregate_banner_only_represents_its_project_and_unbound_briefings(db_url):
    async with _client(db_url) as (client, repo):
        await repo.create_briefing(title="global")
        await repo.create_briefing(title="project one", project_id="p1")
        await repo.create_briefing(title="project two", project_id="p2")
        response = await client.post("/api/notices", json={
            "key": "decisions_pending:project", "catalog_id": "decisions_pending",
            "project_id": "p1", "params": {"n": 99, "title": "old"},
        })
        assert response.status_code == 200
    await close_db()
    async with _client(db_url) as (client, _repo):
        summary = (await client.get("/api/notices/summary")).json()
        assert summary["total"] == 3 and summary["top_count"] == 2 and summary["others"] == 1
        assert "2" in summary["top"]["user_line"]


async def test_empty_aggregate_is_not_a_banner_and_no_top_keeps_other_count(db_url):
    async with _client(db_url) as (client, repo):
        await repo.create_briefing(title="different project", project_id="p2")
        response = await client.post("/api/notices", json={
            "key": "decisions_pending:empty", "catalog_id": "decisions_pending",
            "project_id": "p1", "params": {"n": 3, "title": "stale"},
        })
        assert response.status_code == 200
        await repo.create_task(None, "waiting", config={
            "task_management": {"waiting_on": {"kind": "user", "reason": "决定"}},
        })
    await close_db()
    async with _client(db_url) as (client, _repo):
        summary = (await client.get("/api/notices/summary")).json()
        assert summary["top"] is None
        assert summary["total"] == summary["others"] == 2 and summary["top_count"] == 0


async def test_unknown_kind_is_rejected(db_url):
    async with _client(db_url) as (client, _repo):
        assert (await client.get("/api/notices?kind=action,bogus")).status_code == 400
        assert (await client.get("/api/notices?kind=action")).status_code == 200


async def test_summary_task_waits_match_persisted_wall_waits(db_url, tmp_path):
    async with _client(db_url) as (_client_first, repo):
        project = await repo.create_project(name="waiting", root_path=str(tmp_path / "waiting"))
        other = await repo.create_project(name="other", root_path=str(tmp_path / "other"))
        await repo.create_task(None, "旧分类标签", project_id=project.id, tags=["requires-user-decision"])
        await repo.create_task(None, "等待 agent", project_id=project.id,
                               config={"task_management": {"waiting_on": {"kind": "agent", "reason": "等执行"}}})
        parent = await repo.create_task(None, "等待用户", project_id=project.id,
                                      config={"task_management": {"waiting_on": {"kind": "user", "reason": "批准"}}})
        child = await repo.create_task(None, "子任务等用户", project_id=project.id, parent_id=parent.id,
                                     config={"task_management": {"waiting_on": {"kind": "user", "reason": "选择"}}})
        # A failed task still explicitly waiting for a user remains actionable,
        # exactly as on the wall. Only completed tasks leave the waiting list.
        await repo.update_task(child.id, status=TaskStatus.FAILED)
        await repo.create_task(None, "其他项目等用户", project_id=other.id,
                               config={"task_management": {"waiting_on": {"kind": "user", "reason": "批准"}}})
        done = await repo.create_task(None, "完成", project_id=project.id,
                                     config={"task_management": {"waiting_on": {"kind": "user", "reason": "已决定"}}})
        await repo.update_task(done.id, status=TaskStatus.COMPLETED)
    await close_db()
    async with _client(db_url) as (client, fresh):
        summary = (await client.get("/api/notices/summary")).json()
        assert summary["tasks"] == summary["total"] == 3
        waiting = await fresh.list_waiting_user_tasks(project.id)
        assert {item.id for item in waiting} == {parent.id, child.id}
        scoped = StorageRepository(db_url=db_url, project_scope=project.id)
        assert await scoped.count_waiting_user_tasks() == len(waiting) == 2
        cleared = await client.put(f"/api/tasks/{parent.id}", json={"waiting_on": None})
        assert cleared.status_code == 200, cleared.text
        assert (await client.get("/api/notices/summary")).json()["tasks"] == 2
