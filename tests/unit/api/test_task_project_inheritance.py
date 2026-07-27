"""Tasks created through a team must inherit that team's project.

``POST /api/teams/{id}/tasks/run`` (the ``task_run`` MCP tool) never set
project_id, so every task minted through a team landed with ``project_id=None``
and was invisible to the project task wall (``task_list_project`` /
``GET /api/projects/{id}/task-wall``). Reproduced on the pre-batch-7 tree, so it
predates the tool-face sweep.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from testlib import make_team

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
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    event_bus = EventBus(repo=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = TeamManager(repository=repo, memory=memory)
    deps._hook_translator = HookTranslator(repo=repo, event_bus=event_bus)

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    yield TestClient(app)

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


def _project(name: str, root: str) -> dict:
    return (
        asyncio.get_event_loop()
        .run_until_complete(deps._repository.create_project(name=name, root_path=root))
        .model_dump()
    )


def test_task_run_inherits_team_project(app_client):
    """A task routed through a bound team lands in that team's project."""
    proj = _project("AI team OS", "/tmp/os")
    team = make_team({"name": "impl-team"}, project_id=proj["id"])

    resp = app_client.post(
        f"/api/teams/{team['id']}/tasks/run",
        json={"description": "批 8.5 归属补账", "title": "归属补账"},
    )
    assert resp.status_code == 200
    task_id = resp.json()["data"]["id"]

    task = asyncio.get_event_loop().run_until_complete(deps._repository.get_task(task_id))
    assert task.project_id == proj["id"]


def test_task_run_appears_on_project_task_wall(app_client):
    """The user-visible symptom: project task wall must show the task."""
    proj = _project("AI team OS", "/tmp/os2")
    team = make_team({"name": "wall-team"}, project_id=proj["id"])

    app_client.post(
        f"/api/teams/{team['id']}/tasks/run",
        json={"description": "should be visible", "title": "wall-visible"},
    )

    resp = app_client.get(f"/api/projects/{proj['id']}/task-wall")
    assert resp.status_code == 200
    titles = [t["title"] for bucket in resp.json()["wall"].values() for t in bucket]
    assert "wall-visible" in titles


def test_decompose_parent_and_subtasks_inherit(app_client):
    """The fix sits in create_task, so every team-routed entry point inherits."""
    proj = _project("AI team OS", "/tmp/os3")
    team = make_team({"name": "decompose-team"}, project_id=proj["id"])

    resp = app_client.post(
        f"/api/teams/{team['id']}/tasks/decompose",
        json={
            "title": "拆解任务",
            "description": "父任务",
            "subtasks": [
                {"title": "子一", "description": "a"},
                {"title": "子二", "description": "b"},
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["parent"]["project_id"] == proj["id"]
    assert [c["project_id"] for c in data["subtasks"]] == [proj["id"]] * 2


def test_issue_report_inherits_team_project(app_client):
    proj = _project("AI team OS", "/tmp/os4")
    team = make_team({"name": "issue-team"}, project_id=proj["id"])

    resp = app_client.post(
        f"/api/teams/{team['id']}/issues",
        json={"title": "坏了", "description": "复现步骤", "category": "bug"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["project_id"] == proj["id"]


def test_explicit_project_id_wins_over_team(app_client):
    """An explicit argument still beats the team binding."""
    team_proj = _project("AI team OS", "/tmp/os5")
    other = _project("Wenge", "/tmp/wenge")
    team = make_team({"name": "explicit-team"}, project_id=team_proj["id"])

    task = asyncio.get_event_loop().run_until_complete(
        deps._repository.create_task(
            team_id=team["id"], title="explicit", project_id=other["id"]
        )
    )
    assert task.project_id == other["id"]


def test_task_run_leaves_project_empty_when_team_unbound(app_client):
    """Team with no project -> leave empty rather than guess."""
    team = make_team({"name": "unbound-team"})

    resp = app_client.post(
        f"/api/teams/{team['id']}/tasks/run",
        json={"description": "no home", "title": "no-home"},
    )
    assert resp.status_code == 200
    task_id = resp.json()["data"]["id"]

    task = asyncio.get_event_loop().run_until_complete(deps._repository.get_task(task_id))
    assert task.project_id is None
