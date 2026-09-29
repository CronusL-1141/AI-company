"""The project task wall pages the pending tasks only.

``GET /api/projects/{id}/task-wall`` sorted every open task by score and then cut
the page. Only pending tasks score above 0, so with more pending tasks than the
limit every running and blocked task fell off the page: the session briefing
(limit=20), workflow_reminder (20), task_list_project (50) and the Dashboard (50)
never showed one. Reproduced 2026-09-29 on the real wall: 57 pending, 21 running
and 4 blocked, and every limited read returned pending rows only.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.clock import utc_now
from aiteam.loop.task_wall_engine import TaskWallEngine
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def app_client():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    deps._repository = repo
    deps._event_bus = EventBus(repo=repo)
    deps._task_wall_engine = TaskWallEngine(repo=repo)

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    yield TestClient(app)

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._event_bus = None
    deps._task_wall_engine = None


def _project(root: str) -> str:
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(deps._repository.create_project(name="wall", root_path=root)).id


def _add(client: TestClient, pid: str, title: str, status: str = "pending", **extra: str) -> str:
    resp = client.post(f"/api/projects/{pid}/tasks", json={"title": title, "status": status, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def _rows(body: dict) -> list[dict]:
    return [row for bucket in body["wall"].values() for row in bucket]


@pytest.mark.parametrize("limit", [5, 20, 50])
def test_running_and_blocked_survive_any_pending_page(app_client, tmp_path, limit):
    pid = _project(str(tmp_path / f"p{limit}"))
    for i in range(limit + 10):
        _add(app_client, pid, f"pending-{i}", priority="high", horizon="short")
    running = _add(app_client, pid, "running-one", status="running", horizon="mid")
    blocked = _add(app_client, pid, "blocked-one", status="blocked", horizon="long")

    body = app_client.get(f"/api/projects/{pid}/task-wall?limit={limit}").json()
    rows = _rows(body)
    ids = {row["id"] for row in rows}
    assert running in ids, "a running task fell off the page"
    assert blocked in ids, "a blocked task fell off the page"
    assert sum(row["status"] == "pending" for row in rows) == limit
    assert body["not_shown"] == {"pending": 10}
    assert body["has_more"] is True


def test_offset_pages_pending_and_keeps_the_rest(app_client, tmp_path):
    pid = _project(str(tmp_path / "offset"))
    for i in range(7):
        _add(app_client, pid, f"pending-{i}")
    running = _add(app_client, pid, "running-one", status="running")

    first = app_client.get(f"/api/projects/{pid}/task-wall?limit=5&offset=0").json()
    last = app_client.get(f"/api/projects/{pid}/task-wall?limit=5&offset=5").json()
    first_pending = {r["id"] for r in _rows(first) if r["status"] == "pending"}
    last_pending = {r["id"] for r in _rows(last) if r["status"] == "pending"}
    assert len(first_pending) == 5 and len(last_pending) == 2
    assert not first_pending & last_pending
    assert running in {r["id"] for r in _rows(last)}
    assert (last["not_shown"], last["has_more"]) == ({"pending": 5}, False)


def test_pending_page_comes_first_in_score_order(app_client, tmp_path):
    """Callers that read the first rows as the top of the backlog keep working."""
    pid = _project(str(tmp_path / "order"))
    _add(app_client, pid, "low", priority="low", horizon="short")
    _add(app_client, pid, "busy", status="running", horizon="short")
    _add(app_client, pid, "high", priority="high", horizon="short")

    short = app_client.get(f"/api/projects/{pid}/task-wall").json()["wall"]["short"]
    assert [row["title"] for row in short] == ["high", "low", "busy"]


def test_equal_scores_list_the_task_put_up_first(app_client, tmp_path):
    """The wait boost caps at 3.5 days, so older tasks in one priority x horizon cell
    tie; the tie used to fall to newest-first, leaving the oldest last. Both walls
    now list the task put up first."""
    from testlib import make_team

    pid = _project(str(tmp_path / "fifo"))
    team = make_team({"name": "fifo-team"}, project_id=pid)
    loop = asyncio.get_event_loop()
    for days in (5, 20, 10):
        task = loop.run_until_complete(deps._repository.create_task(
            team_id=team["id"], title=f"{days} days", project_id=pid, priority="high", horizon="short"))
        loop.run_until_complete(deps._repository.update_task(task.id, created_at=utc_now() - timedelta(days=days)))

    project_wall = app_client.get(f"/api/projects/{pid}/task-wall").json()
    assert [row["title"] for row in project_wall["wall"]["short"]] == ["20 days", "10 days", "5 days"]
    assert [item["title"] for item in project_wall["digest"]["top"]] == ["20 days", "10 days", "5 days"]
    team_wall = app_client.get(f"/api/teams/{team['id']}/task-wall").json()
    assert [row["title"] for row in team_wall["wall"]["short"]] == ["20 days", "10 days", "5 days"]
