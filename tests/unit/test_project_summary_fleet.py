"""Tests for the fleet-layer P2 additions to GET /api/projects/{id}/summary.

Covers the two new response fields wired in this batch:
- leaders[].ctx_tokens/ctx_window/ctx_pct/in_flight_tasks (docs/fleet-layer-design.md §6)
- worktrees (docs/worktree-governance-design.md §4/(c))

Both are additive and must degrade silently (existing project_summary tests /
consumers are unaffected when neither signal is available).
"""

from __future__ import annotations

import asyncio
import subprocess
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps, session_probe
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskStatus


def _git(args: list[str], cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture()
def app_client():
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
    yield client, repo

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


def test_summary_worktrees_field_reflects_real_worktree(app_client, tmp_path, monkeypatch):
    client, _repo = app_client

    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git(["init", "-b", "master"], main_repo)
    _git(["config", "user.email", "test@example.com"], main_repo)
    _git(["config", "user.name", "Test"], main_repo)
    (main_repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], main_repo)
    _git(["commit", "-m", "initial"], main_repo)
    wt_path = tmp_path / "wt"
    _git(["worktree", "add", str(wt_path), "-b", "worktree-scenario"], main_repo)

    # No live CC session for this synthetic project dir — keep the probe path a
    # deterministic no-op so this test only exercises the worktrees wiring.
    monkeypatch.setattr(session_probe, "detect_live_sessions", lambda root_path: [])

    resp = client.post("/api/projects", json={"name": "fleet-test", "root_path": str(main_repo)})
    assert resp.status_code == 201
    project_id = resp.json()["data"]["id"]

    summary = client.get(f"/api/projects/{project_id}/summary").json()
    assert "worktrees" in summary
    assert len(summary["worktrees"]) == 1
    assert summary["worktrees"][0]["path"] == str(wt_path)
    assert summary["worktrees"][0]["branch"] == "worktree-scenario"
    assert summary["worktrees"][0]["dirty"] is False
    assert summary["worktrees"][0]["merged"] is True


def test_summary_worktrees_empty_when_no_subordinate_worktree(app_client, tmp_path, monkeypatch):
    client, _repo = app_client
    monkeypatch.setattr(session_probe, "detect_live_sessions", lambda root_path: [])

    resp = client.post("/api/projects", json={"name": "no-wt", "root_path": str(tmp_path)})
    project_id = resp.json()["data"]["id"]

    summary = client.get(f"/api/projects/{project_id}/summary").json()
    assert summary["worktrees"] == []


def test_summary_leader_carries_watermark_and_in_flight_tasks(app_client, tmp_path, monkeypatch):
    client, repo = app_client

    fake_session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(
        session_probe,
        "detect_live_sessions",
        lambda root_path: [
            {
                "session_id": fake_session_id,
                "name": "Atlas",
                "model": "claude-sonnet-5",
                "last_active_at": "2026-07-14T12:00:00",
                "live": True,
                "ctx_tokens": 123_456,
                "ctx_window": 1_000_000,
                "ctx_pct": 12.3,
            }
        ],
    )

    resp = client.post("/api/projects", json={"name": "watermark-test", "root_path": str(tmp_path)})
    project_id = resp.json()["data"]["id"]

    async def _seed():
        team = await repo.create_team(name="fleet-team", project_id=project_id, mode="coordinate")
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader", session_id=fake_session_id
        )
        await repo.update_agent(leader.id, project_id=project_id)
        running_task = await repo.create_task(team_id=team.id, title="running task")
        await repo.update_task(running_task.id, status=TaskStatus.RUNNING)
        await repo.create_task(team_id=team.id, title="pending task")

    asyncio.get_event_loop().run_until_complete(_seed())

    summary = client.get(f"/api/projects/{project_id}/summary").json()
    assert len(summary["leaders"]) == 1
    leader_row = summary["leaders"][0]
    assert leader_row["ctx_tokens"] == 123_456
    assert leader_row["ctx_window"] == 1_000_000
    assert leader_row["ctx_pct"] == 12.3
    assert leader_row["in_flight_tasks"] == 1
