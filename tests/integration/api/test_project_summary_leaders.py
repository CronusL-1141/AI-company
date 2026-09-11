"""Project summaries merge file sessions and persisted leaders without writes."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from aiteam.api import session_probe
from aiteam.api.deps import get_repository
from aiteam.api.routes import projects
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository
from aiteam.types import Agent, AgentStatus, HarnessId

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
CC_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CODEX_SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
async def summary_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, StorageRepository, Path]]:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated_home))
    monkeypatch.setattr(projects, "utc_now", lambda: NOW)
    monkeypatch.setattr(session_probe, "utc_now", lambda: NOW)
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'leaders.db'}"
    repo = StorageRepository(db_url=db_url)
    await repo.init_db()
    app = FastAPI()
    app.include_router(projects.router)
    app.dependency_overrides[get_repository] = lambda: repo
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False,
        ) as client:
            yield client, repo, tmp_path
    finally:
        await get_engine(db_url).dispose()


async def _project(client: httpx.AsyncClient, root: Path) -> str:
    root.mkdir()
    response = await client.post("/api/projects", json={"name": root.name, "root_path": str(root)})
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _transcript(root: Path, session_id: str, *, active_at: datetime, model: str) -> None:
    directory = Path.home() / ".claude" / "projects" / session_probe.project_slug(str(root))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(json.dumps({
        "type": "assistant",
        "message": {"model": model, "usage": {"input_tokens": 100, "output_tokens": 10}},
    }) + "\n", encoding="utf-8")
    os.utime(path, (active_at.timestamp(), active_at.timestamp()))


async def _leader(
    repo: StorageRepository, project_id: str, session_id: str | None,
    *, status: AgentStatus = AgentStatus.BUSY,
    active_at: datetime | None = NOW,
    created_at: datetime = NOW - timedelta(hours=1),
    harness: HarnessId | None = HarnessId.CODEX,
    model: str = "", current_task: str = "", role: str = "leader",
) -> Agent:
    team = await repo.create_team(name=f"fixture-{uuid4().hex}", project_id=project_id, mode="coordinate")
    leader = await repo.create_agent(
        team_id=team.id, name="Leader", role=role, source="hook", session_id=session_id, model=model,
    )
    return await repo.update_agent(
        leader.id, project_id=project_id, status=status, last_active_at=active_at,
        created_at=created_at, harness=harness, current_task=current_task,
    )


async def _summary(client: httpx.AsyncClient, project_id: str) -> dict:
    response = await client.get(f"/api/projects/{project_id}/summary")
    assert response.status_code == 200, response.text
    return response.json()


async def test_old_cc_file_does_not_hide_current_registered_codex_leader(summary_client) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    _transcript(root, CC_SESSION, active_at=NOW - timedelta(days=1), model="observed-cc-model")
    assert (await _summary(client, project_id))["leaders"] == []
    codex = await _leader(repo, project_id, CODEX_SESSION, current_task="Current task")
    before = codex.model_dump(mode="json")

    summary = await _summary(client, project_id)
    leaders = {leader["session_id"]: leader for leader in summary["leaders"]}
    assert set(leaders) == {CODEX_SESSION}
    assert leaders[CODEX_SESSION]["status"] == "busy"
    assert leaders[CODEX_SESSION]["live"] is True
    assert leaders[CODEX_SESSION]["model"] == ""
    assert leaders[CODEX_SESSION]["current_task"] == "Current task"
    assert leaders[CODEX_SESSION]["harness"] == "codex"
    assert summary["leader"] == leaders[CODEX_SESSION]
    assert summary["last_activity_at"] == NOW.isoformat()
    assert await _summary(client, project_id) == summary
    persisted = await repo.get_agent(codex.id)
    assert persisted is not None
    assert persisted.model_dump(mode="json") == before


async def test_registered_active_sessions_are_complete_and_project_scoped(summary_client) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    other_project = await _project(client, tmp_path / "other-project")
    expected = {}
    for session_id, status in (
        (CODEX_SESSION, AgentStatus.BUSY),
        (CC_SESSION, AgentStatus.WAITING),
        ("legacy-observed-session", AgentStatus.OFFLINE),
    ):
        await _leader(repo, project_id, session_id, status=status)
        if status == AgentStatus.BUSY:
            expected[session_id] = status.value
    unknown = await _leader(repo, project_id, "unknown-harness-session", harness=None)
    expected[unknown.session_id] = "busy"
    for missing in (None, "", " "):
        await _leader(repo, project_id, missing)
    await _leader(repo, other_project, "foreign-session")
    await _leader(repo, project_id, "child-session", role="analyst")

    summary = await _summary(client, project_id)
    assert {row["session_id"]: row["status"] for row in summary["leaders"]} == expected
    assert all(row["model"] == "" for row in summary["leaders"])
    assert next(row for row in summary["leaders"] if row["session_id"] == unknown.session_id)["harness"] is None
    assert await _summary(client, project_id) == summary
    persisted = await repo.get_agent(unknown.id)
    assert persisted is not None
    assert persisted.harness is None


async def test_duplicate_session_uses_latest_observation_not_latest_registration(summary_client) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    await _leader(
        repo, project_id, CODEX_SESSION, active_at=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(minutes=3), model="older-observation", current_task="Old task",
    )
    latest = await _leader(
        repo, project_id, CODEX_SESSION, active_at=NOW, created_at=NOW - timedelta(hours=1),
        status=AgentStatus.OFFLINE, model="latest-observation", current_task="",
    )
    summary = await _summary(client, project_id)
    assert summary["leaders"] == []
    assert summary["leader"] is None
    assert await _summary(client, project_id) == summary
    persisted = await repo.get_agent(latest.id)
    assert persisted is not None
    assert persisted.status == AgentStatus.OFFLINE


async def test_each_matching_cc_session_keeps_file_data_and_receives_its_current_task(summary_client) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    second = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    for index, session_id in enumerate((CC_SESSION, second)):
        _transcript(
            root, session_id, active_at=NOW - timedelta(minutes=index), model=f"observed-cc-{index}",
        )
    before = {row["session_id"]: row for row in (await _summary(client, project_id))["leaders"]}
    assert all(row["harness"] == "claude-code" for row in before.values())
    for index, session_id in enumerate((CC_SESSION, second)):
        await _leader(
            repo, project_id, session_id, harness=HarnessId.CLAUDE_CODE,
            status=AgentStatus.WAITING, active_at=NOW - timedelta(seconds=index),
            model="stale-db-model", current_task=f"Task {index}",
        )

    summary = await _summary(client, project_id)
    assert len(summary["leaders"]) == 2
    for index, session_id in enumerate((CC_SESSION, second)):
        expected = before[session_id] | {"current_task": f"Task {index}"}
        assert next(row for row in summary["leaders"] if row["session_id"] == session_id) == expected
    assert await _summary(client, project_id) == summary


@pytest.mark.parametrize(("status", "active_at"), [
    (AgentStatus.OFFLINE, NOW),
    (AgentStatus.BUSY, NOW - timedelta(minutes=16)),
    (AgentStatus.WAITING, None),
    (AgentStatus.BUSY, NOW + timedelta(minutes=1)),
])
async def test_db_status_is_preserved_without_inventing_liveness(summary_client, status, active_at) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    leader = await _leader(repo, project_id, CODEX_SESSION, status=status, active_at=active_at)
    summary = await _summary(client, project_id)
    assert summary["leaders"] == []
    assert summary["leader"] is None
    persisted = await repo.get_agent(leader.id)
    assert persisted is not None
    assert persisted.model_dump(mode="json") == leader.model_dump(mode="json")


async def test_historical_offline_sessions_do_not_fill_current_card(summary_client, monkeypatch) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    _transcript(root, CC_SESSION, active_at=NOW - timedelta(days=1), model="observed-cc-model")
    current = await _leader(repo, project_id, CODEX_SESSION)
    history = [
        await _leader(
            repo, project_id, f"old-session-{index}", status=AgentStatus.OFFLINE,
            active_at=NOW - timedelta(days=30 + index),
        )
        for index in range(4)
    ]

    rows = (await _summary(client, project_id))["leaders"]
    assert {row["session_id"] for row in rows} == {CODEX_SESSION}
    await repo.update_agent(current.id, status=AgentStatus.OFFLINE)
    rows = (await _summary(client, project_id))["leaders"]
    assert rows == []

    monkeypatch.setattr(session_probe, "_claude_projects_dir", lambda: tmp_path / "no-session-files")
    summary = await _summary(client, project_id)
    assert summary["leaders"] == []
    assert summary["leader"] is None
    for leader in [current, *history]:
        persisted = await repo.get_agent(leader.id)
        assert persisted is not None
        assert persisted.status == AgentStatus.OFFLINE
